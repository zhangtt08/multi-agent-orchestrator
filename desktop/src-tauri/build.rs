fn main() {
    // 把仓库根烧进二进制，作为"从 src-tauri/target/debug 里直接跑"时的最后一个兜底。
    // 判据不看这个常量的字面，只看 `<候选目录>/tools/app.py` 在不在。
    let manifest = std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let repo = manifest
        .parent() // desktop/
        .and_then(|p| p.parent()) // 仓库根
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| manifest.clone());
    println!("cargo:rustc-env=MAO_REPO_DIR={}", repo.display());
    tauri_build::build()
}
