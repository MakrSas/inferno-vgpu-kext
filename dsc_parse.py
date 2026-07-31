#!/usr/bin/env python3
"""From-scratch dyld_shared_cache (DSC) parser -- closes this project's long-
standing "no ipsw" gap (see PROJECT_STATUS.md, many sections). Hand-decoded,
same approach as parse_obj.py/resolve.py: no macholib, no ipsw, just
struct.unpack against Apple's own documented layouts.

Format reference: apple-oss-distributions/dyld tag dyld-832.7.1 (the tag
this project's own dyld-crash-investigation section already pinned as
timestamp-matched to this project's xnu build):
  dyld3/shared-cache/dyld_cache_format.h   -- dyld_cache_header,
      dyld_cache_mapping_info, dyld_cache_image_info,
      dyld_cache_image_text_info, dyld_cache_local_symbols_info/_entry
  dyld3/MachOLoaded.cpp (getExportsTrie)   -- how export_off/exportsTrie
      dataoff combine with the image's __LINKEDIT segment to locate the
      per-image export trie
  dyld3/shared-cache/Trie.hpp (processExportNode) -- the export trie's
      on-disk node format (ULEB128 sizes/addresses, no differences from the
      export trie format used in standalone Mach-O LC_DYLD_INFO)

Local copy of that source used while writing this:
  <scratchpad>/dyld_full/  (dyld-832.7.1, full shallow clone)

Two supported queries, both validated against this project's own
independently-known ground truth (_MTLCreateSystemDefaultDevice @
0x1970505d0, from this project's prior live-GDB work, see
PROJECT_STATUS.md's system-wide-patch section):

  sym2addr  <exact-symbol-name> [path-substring-filter]
  addr2sym  <hex-va>

Usage:
  python3 dsc_parse.py sym2addr _MTLCreateSystemDefaultDevice Metal.framework/Metal
  python3 dsc_parse.py addr2sym 0x1970505d0
  python3 dsc_parse.py images [substring]          # list image paths
  python3 dsc_parse.py dump-exports <path-substring> [name-substring]
"""
import mmap
import os
import struct
import sys

DSC_PATH = os.environ.get(
    "DSC_PATH", "/home/makr/Documents/Inferno/InfernoData/dyld_shared_cache_arm64e"
)

LC_SEGMENT_64 = 0x19
LC_SYMTAB = 0x2
LC_DYLD_INFO = 0x22
LC_DYLD_INFO_ONLY = 0x80000022
LC_DYLD_EXPORTS_TRIE = 0x80000033

MH_MAGIC_64 = 0xFEEDFACF


# ---------------------------------------------------------------------------
# dyld_cache_header, field-by-field (explicit offsets, cross-checked against
# a raw `xxd` of the real file's first 0x40 bytes before trusting this):
#   magic[16] @0, mappingOffset/mappingCount/imagesOffset/imagesCount @16
#   (4x uint32), dyldBaseAddress @32 ... through mappingWithSlideCount @316.
#   sizeof(dyld_cache_header) == 320 (0x140) for this format version, which
#   is confirmed live: this file's own mappingOffset field reads exactly
#   0x140, i.e. the mapping array starts immediately after our own decoded
#   header size with zero gap -- the single strongest self-consistency check
#   available without an external reference tool.
# ---------------------------------------------------------------------------
class DSCHeader:
    def __init__(self, data):
        magic = struct.unpack_from("<16s", data, 0)[0]
        self.magic = magic.rstrip(b"\x00").decode(errors="replace")
        (self.mappingOffset, self.mappingCount,
         self.imagesOffset, self.imagesCount) = struct.unpack_from("<IIII", data, 16)
        self.dyldBaseAddress = struct.unpack_from("<Q", data, 32)[0]
        self.codeSignatureOffset, self.codeSignatureSize = struct.unpack_from("<QQ", data, 40)
        self.localSymbolsOffset, self.localSymbolsSize = struct.unpack_from("<QQ", data, 72)
        self.uuid = data[88:104]
        self.cacheType = struct.unpack_from("<Q", data, 104)[0]
        self.imagesTextOffset, self.imagesTextCount = struct.unpack_from("<QQ", data, 136)
        self.platform, self.formatFlags = struct.unpack_from("<II", data, 216)
        self.sharedRegionStart, self.sharedRegionSize, self.maxSlide = \
            struct.unpack_from("<QQQ", data, 224)

    def __repr__(self):
        return (f"<DSCHeader magic={self.magic!r} mappings={self.mappingCount} "
                f"images={self.imagesCount} sharedRegionStart={self.sharedRegionStart:#x} "
                f"localSymbolsOffset={self.localSymbolsOffset:#x} "
                f"localSymbolsSize={self.localSymbolsSize:#x}>")


class Mapping:
    __slots__ = ("address", "size", "fileOffset", "maxProt", "initProt")

    def __init__(self, data, off):
        (self.address, self.size, self.fileOffset,
         self.maxProt, self.initProt) = struct.unpack_from("<QQQII", data, off)


class ImageInfo:
    __slots__ = ("address", "modTime", "inode", "pathFileOffset", "path")

    def __init__(self, data, off):
        (self.address, self.modTime, self.inode,
         self.pathFileOffset, _pad) = struct.unpack_from("<QQQII", data, off)
        end = data.find(b"\x00", self.pathFileOffset)
        self.path = data[self.pathFileOffset:end].decode(errors="replace")


class ImageTextInfo:
    __slots__ = ("uuid", "loadAddress", "textSegmentSize", "pathOffset")

    def __init__(self, data, off):
        self.uuid = data[off:off + 16]
        self.loadAddress, self.textSegmentSize, self.pathOffset = \
            struct.unpack_from("<QII", data, off + 16)


class DSC:
    def __init__(self, path=DSC_PATH):
        self.path = path
        self.f = open(path, "rb")
        self.data = mmap.mmap(self.f.fileno(), 0, prot=mmap.PROT_READ)
        self.hdr = DSCHeader(self.data)
        if self.hdr.magic.split()[0] != "dyld_v1":
            raise ValueError(f"unexpected magic: {self.hdr.magic!r}")

        self.mappings = [Mapping(self.data, self.hdr.mappingOffset + i * 32)
                          for i in range(self.hdr.mappingCount)]

        self.images = [ImageInfo(self.data, self.hdr.imagesOffset + i * 32)
                        for i in range(self.hdr.imagesCount)]
        self.images.sort(key=lambda im: im.address)
        self._image_addrs = [im.address for im in self.images]

        self.images_text = []
        if self.hdr.imagesTextOffset and self.hdr.imagesTextCount:
            self.images_text = [ImageTextInfo(self.data, self.hdr.imagesTextOffset + i * 32)
                                 for i in range(self.hdr.imagesTextCount)]
            self.images_text.sort(key=lambda it: it.loadAddress)

        # sanity: confirm the "flat file" assumption (fileOffset == address -
        # sharedRegionStart for every mapping) that lets per-image Mach-O
        # load-command fileoff fields be used directly as real DSC file
        # offsets, exactly like MachOLoaded::getExportsTrie's live-pointer
        # arithmetic collapses to when address deltas and file-offset deltas
        # are identical across the whole cache (true for this pre-split,
        # single-file iOS 14 cache; NOT generally true for post-iOS16
        # sub-caches).
        self.flat = all(
            m.fileOffset == m.address - self.hdr.sharedRegionStart for m in self.mappings
        )

    def close(self):
        self.data.close()
        self.f.close()

    # -- vmaddr <-> file offset -------------------------------------------------
    def vm_to_file(self, vmaddr):
        for m in self.mappings:
            if m.address <= vmaddr < m.address + m.size:
                return m.fileOffset + (vmaddr - m.address)
        return None

    # -- which image owns a given VA --------------------------------------------
    def image_for_addr(self, vmaddr):
        """Prefer the precise __TEXT range table (dyld_cache_image_text_info);
        fall back to 'nearest image whose mach_header address is <= va' from
        the classic dyld_cache_image_info list if the address isn't in any
        image's __TEXT (e.g. it's in __DATA)."""
        for it in self.images_text:
            if it.loadAddress <= vmaddr < it.loadAddress + it.textSegmentSize:
                for im in self.images:
                    if im.address == it.loadAddress:
                        return im
        import bisect
        i = bisect.bisect_right(self._image_addrs, vmaddr) - 1
        if i < 0:
            return None
        return self.images[i]

    def find_image(self, path_substr):
        matches = [im for im in self.images if path_substr in im.path]
        return matches

    # -- per-image Mach-O load-command walk --------------------------------------
    def _parse_image_lcs(self, image):
        off0 = self.vm_to_file(image.address)
        magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, _res = \
            struct.unpack_from("<IiiIIIII", self.data, off0)
        if magic != MH_MAGIC_64:
            raise ValueError(f"{image.path}: bad mach_header magic {magic:#x} @ file {off0:#x}")

        info = {"text_vmaddr": None, "linkedit_vmaddr": None,
                "linkedit_fileoff": None, "linkedit_filesize": None,
                "dyld_info": None, "exports_trie": None, "symtab": None}

        off = off0 + 32
        for _ in range(ncmds):
            cmd, cmdsize = struct.unpack_from("<II", self.data, off)
            if cmd == LC_SEGMENT_64:
                segname, vmaddr, vmsize, fileoff, filesize, maxprot, initprot, nsects, sflags = \
                    struct.unpack_from("<16sQQQQiiII", self.data, off + 8)
                segname = segname.rstrip(b"\x00").decode(errors="replace")
                if segname == "__TEXT" and info["text_vmaddr"] is None:
                    info["text_vmaddr"] = vmaddr
                elif segname == "__LINKEDIT":
                    info["linkedit_vmaddr"] = vmaddr
                    info["linkedit_fileoff"] = fileoff
                    info["linkedit_filesize"] = filesize
            elif cmd in (LC_DYLD_INFO, LC_DYLD_INFO_ONLY):
                fields = struct.unpack_from("<IIIIIIIIIIII", self.data, off + 8)
                # rebase_off/size, bind_off/size, weak_bind_off/size,
                # lazy_bind_off/size, export_off/size
                info["dyld_info"] = {"export_off": fields[10], "export_size": fields[11]}
            elif cmd == LC_DYLD_EXPORTS_TRIE:
                dataoff, datasize = struct.unpack_from("<II", self.data, off + 8)
                info["exports_trie"] = {"dataoff": dataoff, "datasize": datasize}
            elif cmd == LC_SYMTAB:
                symoff, nsyms, stroff, strsize = struct.unpack_from("<IIII", self.data, off + 8)
                info["symtab"] = {"symoff": symoff, "nsyms": nsyms,
                                   "stroff": stroff, "strsize": strsize}
            off += cmdsize
        return info

    def _exports_trie_bytes(self, image, info):
        """Port of MachOLoaded::getExportsTrie: prefer LC_DYLD_EXPORTS_TRIE,
        fall back to LC_DYLD_INFO[_ONLY]'s export_off/export_size. Both
        offsets are file offsets *as if the whole file were the classic
        single flat mapping* (offsetInLinkEdit = raw_off - linkedit_fileoff,
        then re-based onto wherever __LINKEDIT's own vmaddr maps to in real
        file-offset space via vm_to_file) -- this is the general form that
        still works even where the flat-file shortcut doesn't hold."""
        if info["linkedit_vmaddr"] is None:
            return None, 0
        linkedit_file_base = self.vm_to_file(info["linkedit_vmaddr"])
        if linkedit_file_base is None:
            return None, 0

        if info["exports_trie"] is not None:
            raw_off = info["exports_trie"]["dataoff"]
            size = info["exports_trie"]["datasize"]
        elif info["dyld_info"] is not None:
            raw_off = info["dyld_info"]["export_off"]
            size = info["dyld_info"]["export_size"]
        else:
            return None, 0
        if size == 0:
            return None, 0
        offset_in_linkedit = raw_off - info["linkedit_fileoff"]
        trie_file_off = linkedit_file_base + offset_in_linkedit
        return self.data[trie_file_off:trie_file_off + size], trie_file_off

    # -- export trie walk (port of Trie.hpp::processExportNode) -----------------
    @staticmethod
    def _uleb128(buf, p):
        result = 0
        bit = 0
        while True:
            b = buf[p]
            p += 1
            result |= (b & 0x7F) << bit
            if not (b & 0x80):
                break
            bit += 7
        return result, p

    def _walk_trie(self, trie, image_base, want_name=None, collect=None, prefix=b""):
        """Recursive export-trie walk. If want_name is set, returns (name,
        addr, flags) for that exact symbol or None. If collect is a list,
        appends (name, addr, flags) for every terminal node found."""
        stack = [(0, prefix)]
        found = None
        while stack:
            node_off, cum = stack.pop()
            p = node_off
            terminal_size, p = self._uleb128(trie, p)
            children_start = p + terminal_size
            if terminal_size != 0:
                tp = p
                flags, tp = self._uleb128(trie, tp)
                addr = 0
                if not (flags & 0x8):  # EXPORT_SYMBOL_FLAGS_REEXPORT
                    addr, tp = self._uleb128(trie, tp)
                name = cum.decode(errors="replace")
                if want_name is not None and name == want_name:
                    return (name, image_base + addr, flags)
                if collect is not None:
                    collect.append((name, image_base + addr, flags))
            p = children_start
            n_children = trie[p]
            p += 1
            for _ in range(n_children):
                start = p
                end = trie.index(b"\x00", start)
                edge = trie[start:end]
                p = end + 1
                child_off, p = self._uleb128(trie, p)
                stack.append((child_off, cum + edge))
        return found

    # -- public API ---------------------------------------------------------
    def sym2addr(self, name, path_filter=None):
        candidates = self.images
        if path_filter:
            candidates = self.find_image(path_filter)
        for im in candidates:
            try:
                info = self._parse_image_lcs(im)
                trie, _ = self._exports_trie_bytes(im, info)
            except Exception:
                continue
            if not trie:
                continue
            hit = self._walk_trie(trie, im.address, want_name=name)
            if hit:
                return hit + (im,)
        return None

    def dump_exports(self, path_filter, name_filter=None):
        out = []
        for im in self.find_image(path_filter):
            try:
                info = self._parse_image_lcs(im)
                trie, _ = self._exports_trie_bytes(im, info)
            except Exception:
                continue
            if not trie:
                continue
            collected = []
            self._walk_trie(trie, im.address, collect=collected)
            for name, addr, flags in collected:
                if name_filter is None or name_filter in name:
                    out.append((name, addr, flags, im.path))
        return out

    def addr2sym(self, vmaddr):
        im = self.image_for_addr(vmaddr)
        if im is None:
            return None
        info = self._parse_image_lcs(im)
        trie, _ = self._exports_trie_bytes(im, info)
        collected = []
        if trie:
            self._walk_trie(trie, im.address, collect=collected)

        # also pull this image's LOCAL (non-exported) symbols out of the
        # cache-wide local-symbols blob, if present -- gives much finer
        # nearest-symbol resolution than exports alone (most internal
        # helper functions, e.g. block_invoke thunks, are never exported).
        collected.extend(self._local_symbols_for_image(im))

        if not collected:
            return (None, None, im, None)
        best = None
        for name, addr, flags in collected:
            if addr <= vmaddr and (best is None or addr > best[1]):
                best = (name, addr, flags)
        if best is None:
            return (None, None, im, None)
        return (best[0], vmaddr - best[1], im, best[1])

    # -- cache-wide local symbols (dyld_cache_local_symbols_info) ---------------
    _local_cache = None

    def _load_local_symbols_index(self):
        if self._local_cache is not None:
            return self._local_cache
        self._local_cache = {}
        if not (self.hdr.localSymbolsOffset and self.hdr.localSymbolsSize):
            return self._local_cache
        base = self.hdr.localSymbolsOffset
        nlistOffset, nlistCount, stringsOffset, stringsSize, entriesOffset, entriesCount = \
            struct.unpack_from("<IIIIII", self.data, base)
        for i in range(entriesCount):
            eo = base + entriesOffset + i * 12
            dylibOffset, nlistStartIndex, nlistCount_i = struct.unpack_from("<III", self.data, eo)
            self._local_cache[dylibOffset] = (nlistStartIndex, nlistCount_i)
        self._local_meta = (base, nlistOffset, stringsOffset, stringsSize)
        return self._local_cache

    def _local_symbols_for_image(self, image):
        idx = self._load_local_symbols_index()
        if not idx:
            return []
        file_off = self.vm_to_file(image.address)
        entry = idx.get(file_off)
        if entry is None:
            return []
        base, nlistOffset, stringsOffset, stringsSize = self._local_meta
        start_i, count = entry
        out = []
        for i in range(count):
            no = base + nlistOffset + (start_i + i) * 16
            n_strx, n_type, n_sect, n_desc, n_value = struct.unpack_from("<IBBHQ", self.data, no)
            if n_value == 0:
                continue
            name_off = base + stringsOffset + n_strx
            end = self.data.find(b"\x00", name_off)
            name = self.data[name_off:end].decode(errors="replace")
            out.append((name, n_value, 0))
        return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    dsc = DSC()
    print(f"# {DSC_PATH}", file=sys.stderr)
    print(f"# {dsc.hdr!r} flat={dsc.flat}", file=sys.stderr)

    if cmd == "sym2addr":
        name = sys.argv[2]
        path_filter = sys.argv[3] if len(sys.argv) > 3 else None
        hit = dsc.sym2addr(name, path_filter)
        if hit is None:
            print(f"NOT FOUND: {name}")
            sys.exit(2)
        found_name, addr, flags, im = hit
        print(f"{found_name} = {addr:#x}  flags={flags:#x}  image={im.path}")
    elif cmd == "addr2sym":
        va = int(sys.argv[2], 16)
        name, delta, im, sym_addr = dsc.addr2sym(va)
        if name is None:
            print(f"{va:#x}: no symbol found (image={im.path if im else None})")
        else:
            print(f"{va:#x} = {name} + {delta:#x}   (sym@{sym_addr:#x})  image={im.path}")
    elif cmd == "images":
        substr = sys.argv[2] if len(sys.argv) > 2 else ""
        for im in dsc.images:
            if substr in im.path:
                print(f"{im.address:#018x}  {im.path}")
    elif cmd == "dump-exports":
        path_filter = sys.argv[2]
        name_filter = sys.argv[3] if len(sys.argv) > 3 else None
        for name, addr, flags, path in dsc.dump_exports(path_filter, name_filter):
            print(f"{addr:#018x}  {name}   [{path}]")
    else:
        print(f"unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
