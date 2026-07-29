//! Listens on a Unix socket for INFERNO_VGPU_OP_COMPUTE_DISPATCH requests
//! forwarded by the QEMU inferno-vgpu device model, translates the AIR
//! payload via metal2vulkan, dispatches it through reims-vgpu's Vulkan
//! engine on the real host GPU, and replies with the buffer's post-dispatch
//! contents. See inferno-vgpu.h for the exact wire format both sides agree
//! on (kept intentionally tiny: one buffer, always binding 0, always
//! writable -- this is the smallest slice that proves the whole chain works
//! before generalizing).
use metal2vulkan::passes::Stage;
use reims_vgpu::backend::vulkan::engine::{execute_compute, ComputeBufferResource, ComputeRequest};
use std::io::{Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};

const SOCK_PATH: &str = "/tmp/inferno-render.sock";

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

fn handle_request(stream: &mut UnixStream) -> std::io::Result<()> {
    let air_len = read_u32(stream)? as usize;
    let air_bytes = read_exact_into(stream, (air_len + 3) & !3)?;
    let buf_len = read_u32(stream)? as usize;
    let buf_bytes = read_exact_into(stream, (buf_len + 3) & !3)?;

    let result = run_dispatch(&air_bytes[..air_len], &buf_bytes[..buf_len]);

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

fn run_dispatch(air_text: &[u8], buf_bytes: &[u8]) -> Result<Vec<u8>, String> {
    let air_str = std::str::from_utf8(air_text).map_err(|e| format!("AIR not utf8: {e}"))?;
    let tmp = std::env::temp_dir().join(format!("inferno_render_{}", std::process::id()));
    std::fs::create_dir_all(&tmp).map_err(|e| e.to_string())?;
    let ll_path = tmp.join("shader.ll");
    std::fs::write(&ll_path, air_str).map_err(|e| e.to_string())?;

    let spirv_bytes = metal2vulkan::translate(ll_path.to_str().unwrap(), Stage::Kernel, &tmp)?;
    let spirv_words = spirv_bytes_to_words(&spirv_bytes);

    let req = ComputeRequest {
        spirv: spirv_words,
        entry: "main".to_string(),
        grid: [1, 1, 1],
        storage_buffers: vec![ComputeBufferResource {
            binding: 0,
            bytes: buf_bytes.to_vec(),
            writable: true,
        }],
        ..Default::default()
    };

    let out = execute_compute(req).map_err(|e| format!("{e:?}"))?;
    let _ = std::fs::remove_dir_all(&tmp);
    out.buffers
        .into_iter()
        .find(|b| b.binding == 0)
        .map(|b| b.bytes)
        .ok_or_else(|| "no output for binding 0".to_string())
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
