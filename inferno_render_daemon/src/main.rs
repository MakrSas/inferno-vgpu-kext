//! Listens on a Unix socket for INFERNO_VGPU_OP_COMPUTE_DISPATCH /
//! INFERNO_VGPU_OP_DRAW requests forwarded by the QEMU inferno-vgpu device
//! model, translates the AIR payload(s) via metal2vulkan, dispatches them
//! through reims-vgpu's Vulkan engine on the real host GPU, and replies with
//! the result bytes. See inferno-vgpu.h for the exact wire formats both
//! sides agree on.
use metal2vulkan::passes::Stage;
use reims_vgpu::backend::vulkan::engine::{
    execute_compute, execute_draw, BufferContent, ComputeBufferResource, ComputeRequest,
    DrawRequest, VertexAttributeFormat, VertexAttributeResource, VertexStepFunction,
};
use std::io::{Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::Arc;

const SOCK_PATH: &str = "/tmp/inferno-render.sock";
const OP_COMPUTE_DISPATCH: u32 = 0x0002;
const OP_DRAW: u32 = 0x0003;

fn read_exact_into(stream: &mut UnixStream, len: usize) -> std::io::Result<Vec<u8>> {
    let mut buf = vec![0u8; len];
    stream.read_exact(&mut buf)?;
    Ok(buf)
}

fn read_u32(stream: &mut UnixStream) -> std::io::Result<u32> {
    let mut b = [0u8; 4];
    stream.read_exact(&mut b)?;
    Ok(u32::from_le_bytes(b))
}

fn spirv_bytes_to_words(bytes: &[u8]) -> Vec<u32> {
    bytes
        .chunks_exact(4)
        .map(|c| u32::from_ne_bytes([c[0], c[1], c[2], c[3]]))
        .collect()
}

fn translate_air(air_text: &[u8], stage: Stage, tag: &str) -> Result<Vec<u32>, String> {
    let air_str = std::str::from_utf8(air_text).map_err(|e| format!("AIR not utf8: {e}"))?;
    let tmp = std::env::temp_dir().join(format!("inferno_render_{}_{}", std::process::id(), tag));
    std::fs::create_dir_all(&tmp).map_err(|e| e.to_string())?;
    let ll_path = tmp.join("shader.ll");
    std::fs::write(&ll_path, air_str).map_err(|e| e.to_string())?;
    let spirv_bytes = metal2vulkan::translate(ll_path.to_str().unwrap(), stage, &tmp)?;
    let _ = std::fs::remove_dir_all(&tmp);
    Ok(spirv_bytes_to_words(&spirv_bytes))
}

fn handle_request(stream: &mut UnixStream) -> std::io::Result<()> {
    let opcode = read_u32(stream)?;

    let result = match opcode {
        OP_COMPUTE_DISPATCH => handle_compute(stream),
        OP_DRAW => handle_draw(stream),
        other => Err(format!("unknown opcode 0x{other:x}")),
    };

    let mut reply = Vec::new();
    match result {
        Ok(out_bytes) => {
            reply.extend_from_slice(&0u32.to_le_bytes());
            reply.extend_from_slice(&(out_bytes.len() as u32).to_le_bytes());
            reply.extend_from_slice(&out_bytes);
        }
        Err(e) => {
            eprintln!("dispatch failed: {e}");
            reply.extend_from_slice(&1u32.to_le_bytes());
            reply.extend_from_slice(&0u32.to_le_bytes());
        }
    }
    stream.write_all(&reply)
}

// Wire format (inferno-vgpu.h): u32 air_len, air_bytes[padded], u32 buf_len, buf_bytes[padded].
fn handle_compute(stream: &mut UnixStream) -> Result<Vec<u8>, String> {
    let air_len = read_u32(stream).map_err(|e| e.to_string())? as usize;
    let air_bytes = read_exact_into(stream, (air_len + 3) & !3).map_err(|e| e.to_string())?;
    let buf_len = read_u32(stream).map_err(|e| e.to_string())? as usize;
    let buf_bytes = read_exact_into(stream, (buf_len + 3) & !3).map_err(|e| e.to_string())?;

    let spirv_words = translate_air(&air_bytes[..air_len], Stage::Kernel, "compute")?;

    let req = ComputeRequest {
        spirv: spirv_words,
        entry: "main".to_string(),
        grid: [1, 1, 1],
        storage_buffers: vec![ComputeBufferResource {
            binding: 0,
            bytes: buf_bytes[..buf_len].to_vec(),
            writable: true,
        }],
        ..Default::default()
    };

    let out = execute_compute(req).map_err(|e| format!("{e:?}"))?;
    out.buffers
        .into_iter()
        .find(|b| b.binding == 0)
        .map(|b| b.bytes)
        .ok_or_else(|| "no output for binding 0".to_string())
}

// Wire format (inferno-vgpu.h): u32 vert_air_len, vert_air_bytes[padded],
// u32 frag_air_len, frag_air_bytes[padded], u32 vbuf_len, vbuf_bytes[padded],
// u32 width, u32 height, u32 vertex_count.
fn handle_draw(stream: &mut UnixStream) -> Result<Vec<u8>, String> {
    let vert_air_len = read_u32(stream).map_err(|e| e.to_string())? as usize;
    let vert_air = read_exact_into(stream, (vert_air_len + 3) & !3).map_err(|e| e.to_string())?;
    let frag_air_len = read_u32(stream).map_err(|e| e.to_string())? as usize;
    let frag_air = read_exact_into(stream, (frag_air_len + 3) & !3).map_err(|e| e.to_string())?;
    let vbuf_len = read_u32(stream).map_err(|e| e.to_string())? as usize;
    let vbuf = read_exact_into(stream, (vbuf_len + 3) & !3).map_err(|e| e.to_string())?;
    let width = read_u32(stream).map_err(|e| e.to_string())?;
    let height = read_u32(stream).map_err(|e| e.to_string())?;
    let vertex_count = read_u32(stream).map_err(|e| e.to_string())?;

    let vert_spirv = translate_air(&vert_air[..vert_air_len], Stage::Vertex, "vert")?;
    let frag_spirv = translate_air(&frag_air[..frag_air_len], Stage::Fragment, "frag")?;

    let req = DrawRequest {
        vert_spirv: Arc::new(vert_spirv),
        frag_spirv: Arc::new(frag_spirv),
        width,
        height,
        vertex_count,
        vertex_attributes: vec![VertexAttributeResource {
            location: 0,
            binding: 0,
            format: VertexAttributeFormat::Float4,
            offset: 0,
            stride: 16,
            step_function: VertexStepFunction::PerVertex,
            step_rate: 1,
            content: BufferContent::Bytes(Arc::new(vbuf[..vbuf_len].to_vec())),
        }],
        flip_viewport_y: true,
        ..Default::default()
    };

    let out = execute_draw(req).map_err(|e| format!("{e:?}"))?;
    Ok(out.pixels)
}

fn main() {
    let _ = std::fs::remove_file(SOCK_PATH);
    let listener = UnixListener::bind(SOCK_PATH).expect("bind socket");
    println!("inferno-render-daemon listening on {SOCK_PATH}");

    for conn in listener.incoming() {
        match conn {
            Ok(mut stream) => {
                if let Err(e) = handle_request(&mut stream) {
                    eprintln!("request error: {e}");
                }
            }
            Err(e) => eprintln!("accept error: {e}"),
        }
    }
}
