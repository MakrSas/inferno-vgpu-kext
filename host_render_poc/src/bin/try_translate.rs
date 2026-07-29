//! Fast local iteration: translate a .ll file and run spirv-val, without
//! touching the guest at all. Usage: try_translate <in.ll> <vertex|fragment|kernel>
use metal2vulkan::passes::Stage;

fn main() {
    let mut args = std::env::args().skip(1);
    let path = args.next().expect("usage: try_translate <in.ll> <stage>");
    let stage = match args.next().as_deref() {
        Some("vertex") => Stage::Vertex,
        Some("fragment") => Stage::Fragment,
        _ => Stage::Kernel,
    };
    let tmp = std::env::temp_dir().join(format!("try_translate_{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    match metal2vulkan::translate(&path, stage, &tmp) {
        Ok(spv) => {
            println!("PASS: {} bytes of SPIR-V", spv.len());
            match metal2vulkan::tools::spirv_val_bytes(&spv, &tmp) {
                Ok(()) => println!("PASS spirv-val"),
                Err(e) => println!("INVALID-SPIRV: {e}"),
            }
        }
        Err(e) => println!("FALLBACK: {e}"),
    }
    let _ = std::fs::remove_dir_all(&tmp);
}
