//! Proof of concept: metal2vulkan (AIR -> SPIR-V) feeding straight into
//! reims-vgpu's Vulkan compute engine, with NO Apple wire-protocol decoding
//! involved -- ComputeRequest takes plain SPIR-V + byte buffers. This is the
//! reusable "back half" for Inferno's own, much simpler guest<->host
//! protocol: our own kext/bridge code (already built and validated this
//! session) supplies the AIR + buffer bytes; this engine does the rest.

use metal2vulkan::passes::Stage;
use reims_vgpu::backend::vulkan::engine::{
    execute_compute, ComputeBufferResource, ComputeRequest,
};

fn spirv_bytes_to_words(bytes: &[u8]) -> Vec<u32> {
    bytes
        .chunks_exact(4)
        .map(|c| u32::from_ne_bytes([c[0], c[1], c[2], c[3]]))
        .collect()
}

fn main() {
    let fixture = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "kernel_store_const.ll".to_string());

    let tmp = std::env::temp_dir().join(format!("host_render_poc_{}", std::process::id()));
    std::fs::create_dir_all(&tmp).expect("create tmp dir");

    println!("translating {fixture} via metal2vulkan...");
    let spirv_bytes = metal2vulkan::translate(&fixture, Stage::Kernel, &tmp)
        .unwrap_or_else(|e| panic!("metal2vulkan translate failed: {e}"));
    println!("PASS: got {} bytes of SPIR-V", spirv_bytes.len());

    let spirv_words = spirv_bytes_to_words(&spirv_bytes);

    // kernel_store_const: `void store_const(device int* out [[buffer(0)]])`
    // stores the constant 42 into out[0]. Seed 4 zero bytes, dispatch a
    // single 1x1x1 workgroup, then check the readback.
    let req = ComputeRequest {
        spirv: spirv_words,
        entry: "main".to_string(),
        grid: [1, 1, 1],
        storage_buffers: vec![ComputeBufferResource {
            binding: 0,
            bytes: vec![0u8; 4],
            writable: true,
        }],
        ..Default::default()
    };

    println!("dispatching compute via reims-vgpu's Vulkan engine...");
    let out = execute_compute(req).unwrap_or_else(|e| panic!("execute_compute failed: {e:?}"));

    let result_bytes = &out.buffers.iter().find(|b| b.binding == 0).expect("binding 0 in output").bytes;
    let result = i32::from_ne_bytes(result_bytes[0..4].try_into().unwrap());
    println!("readback: buffer[0] = {result} (expected 42)");
    assert_eq!(result, 42, "real GPU compute dispatch did not produce the expected result");
    println!("PASS: real Vulkan compute dispatch on host GPU produced the correct result");

    let _ = std::fs::remove_dir_all(&tmp);
}
