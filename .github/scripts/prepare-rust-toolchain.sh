#!/usr/bin/env bash
set -euo pipefail

root="${KERNEL_ROOT:?KERNEL_ROOT 未设置}"
common="${1:-$root/common}"
constants="$common/build.config.constants"

log() {
  printf '%s\n' "$*" >&2
}

if [ ! -f "$constants" ]; then
  log "缺少 $constants，无法确定 Rust 工具链版本"
  exit 1
fi

rustc_ver=$(sed -n 's/^RUSTC_VERSION=//p' "$constants" | head -n1 | tr -d '[:space:]')
clang_ver=$(sed -n 's/^CLANG_VERSION=//p' "$constants" | head -n1 | tr -d '[:space:]')
if [ -z "$rustc_ver" ] || [ -z "$clang_ver" ]; then
  log "build.config.constants 缺少 RUSTC_VERSION 或 CLANG_VERSION"
  exit 1
fi

rust_dir="$root/prebuilts/rust/linux-x86/$rustc_ver"

rust_tmp=""
cleanup_rust_tmp() {
  [ -z "$rust_tmp" ] || rm -rf "$rust_tmp"
}
trap cleanup_rust_tmp EXIT

install_rust_prebuilt() {
  local src available base
  rust_tmp=$(mktemp -d)
  if ! git clone --depth 1 --filter=blob:none --sparse --branch rust-1.82-release \
      https://android.googlesource.com/platform/prebuilts/rust "$rust_tmp" >&2; then
    log "克隆 prebuilts/rust 失败"
    return 1
  fi
  available=$(git -C "$rust_tmp" ls-tree --name-only HEAD linux-x86/ | sed 's#^linux-x86/##; s#/$##' | grep -E '^[0-9]' | sort -V || true)
  src="$rustc_ver"
  if ! printf '%s\n' "$available" | grep -qx "$src"; then
    base="${rustc_ver%.*}"
    src=$(printf '%s\n' "$available" | awk -F. -v b="$base" '$1"."$2 == b' | sort -V | tail -n1 || true)
  fi
  if [ -z "$src" ]; then
    log "prebuilts/rust 中找不到与 $rustc_ver 匹配的版本"
    return 1
  fi
  git -C "$rust_tmp" sparse-checkout set "linux-x86/$src"
  mkdir -p "$(dirname "$rust_dir")"
  cp -a "$rust_tmp/linux-x86/$src" "$rust_dir"
  log "已安装 Rust 预编译工具链 $src 到 $rust_dir"
}

rustc="$rust_dir/bin/rustc"
rust_lib_src="$rust_dir/lib/rustlib/src/rust/library"

if [ ! -x "$rustc" ] || [ ! -d "$rust_lib_src" ]; then
  rm -rf "$rust_dir"
  install_rust_prebuilt
fi

bindgen="$root/prebuilts/clang-tools/linux-x86/bin/bindgen"
clang_root="$root/prebuilts/clang/host/linux-x86"
libclang="$clang_root/clang-$clang_ver/lib64"
if [ ! -d "$libclang" ]; then
  fallback=""
  for candidate in "$clang_root"/clang-*/lib64 "$clang_root"/clang-*/lib; do
    [ -d "$candidate" ] && fallback="$candidate" && break
  done
  if [ -n "$fallback" ]; then
    log "pin 的 clang-$clang_ver/lib64 缺失，改用 $fallback"
    libclang="$fallback"
  fi
fi

[ -x "$rustc" ] || { log "缺少 $rustc"; exit 1; }
[ -d "$rust_lib_src" ] || { log "$rust_dir 缺少 rust-src"; exit 1; }
[ -x "$bindgen" ] || { log "缺少 $bindgen"; exit 1; }
[ -d "$libclang" ] || { log "缺少 $libclang（prebuilts/clang 未同步或结构异常）"; exit 1; }

printf 'export RUSTC=%q\n' "$rustc"
printf 'export RUST_PREBUILT_BIN=%q\n' "prebuilts/rust/linux-x86/$rustc_ver/bin"
printf 'export RUST_LIB_SRC=%q\n' "$rust_lib_src"
printf 'export BINDGEN=%q\n' "$bindgen"
printf 'export LIBCLANG_PATH=%q\n' "$libclang"
printf 'export LD_LIBRARY_PATH=%q\n' "$libclang:${LD_LIBRARY_PATH:-}"
printf 'export PATH=%q\n' "$rust_dir/bin:${PATH}"