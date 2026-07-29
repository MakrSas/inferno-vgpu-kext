//! Local (host-only) proof that a real vertex+fragment draw works through
//! reims-vgpu's Vulkan engine before ever wiring this into the guest
//! pipeline. Draws one red triangle into a small offscreen target and
//! writes it out as a .ppm so it can be eyeballed.
use metal2vulkan::passes::Stage;
use reims_vgpu::backend::vulkan::engine::{
    execute_draw, DrawRequest, VertexAttributeFormat, VertexAttributeResource,
    VertexStepFunction,
};
use std::sync::Arc;

fn spirv_bytes_to_words(bytes: &[u8]) -> Vec<u32> {
    bytes
        .chunks_exact(4)
        .map(|c| u32::from_ne_bytes([c[0], c[1], c[2], c[3]]))
        .collect()
}

fn translate(path: &str, stage: Stage) -> Vec<u32> {
    let tmp = std::env::temp_dir().join(format!("try_draw_{}_{:?}", std::process::id(), stage));
    std::fs::create_dir_all(&tmp).unwrap();
    let spv = metal2vulkan::translate(path, stage, &tmp)
        .unwrap_or_else(|e| panic!("translate {path} failed: {e}"));
    let _ = std::fs::remove_dir_all(&tmp);
    spirv_bytes_to_words(&spv)
}

fn main() {
    let vert = translate("vertex_passthrough.ll", Stage::Vertex);
    let frag = translate("fragment_solid_red.ll", Stage::Fragment);
    println!("vert spirv: {} words, frag spirv: {} words", vert.len(), frag.len());

    let width = 64u32;
    let height = 64u32;

    // Clip-space triangle: top, bottom-left, bottom-right. Each vertex is a
    // float4 (x,y,z,w) -- matches vertex_passthrough.ll's single float4
    // "position" attribute, passed straight through to [[position]].
    let verts: [[f32; 4]; 3] = [
        [0.0, 0.6, 0.0, 1.0],
        [-0.6, -0.6, 0.0, 1.0],
        [0.6, -0.6, 0.0, 1.0],
    ];
    let mut vertex_bytes = Vec::with_capacity(3 * 16);
    for v in &verts {
        for f in v {
            vertex_bytes.extend_from_slice(&f.to_le_bytes());
        }
    }

    let req = DrawRequest {
        vert_spirv: Arc::new(vert),
        frag_spirv: Arc::new(frag),
        width,
        height,
        vertex_count: 3,
        vertex_attributes: vec![VertexAttributeResource {
            location: 0,
            binding: 0,
            format: VertexAttributeFormat::Float4,
            offset: 0,
            stride: 16,
            step_function: VertexStepFunction::PerVertex,
            step_rate: 1,
            content: reims_vgpu::backend::vulkan::engine::BufferContent::Bytes(Arc::new(
                vertex_bytes,
            )),
        }],
        ..Default::default()
    };

    println!("dispatching draw via reims-vgpu's Vulkan engine...");
    let out = execute_draw(req).unwrap_or_else(|e| panic!("execute_draw failed: {e:?}"));
    println!("PASS: got {} bytes of pixels (expect {})", out.pixels.len(), width * height * 4);

    // Quick sanity check: center pixel should be red-ish (inside the
    // triangle), a corner pixel should be black (outside, cleared).
    let idx = |x: u32, y: u32| ((y * width + x) * 4) as usize;
    let center = &out.pixels[idx(width / 2, height / 2)..idx(width / 2, height / 2) + 4];
    let corner = &out.pixels[idx(2, 2)..idx(2, 2) + 4];
    println!("center pixel RGBA = {:?}", center);
    println!("corner pixel RGBA = {:?}", corner);

    std::fs::write(
        "/tmp/try_draw_out.ppm",
        {
            let mut ppm = format!("P6\n{width} {height}\n255\n").into_bytes();
            for chunk in out.pixels.chunks_exact(4) {
                ppm.extend_from_slice(&chunk[0..3]);
            }
            ppm
        },
    )
    .unwrap();
    println!("wrote /tmp/try_draw_out.ppm");
}
