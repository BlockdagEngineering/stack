#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RELEASE_ROOT="${BDAG_RELEASE_ROOT:-/home/jeremy/blockdag-releases}"
STAMP="${BDAG_RELEASE_STAMP:-$(date +%Y%m%d-%H%M%S)}"
RELEASE_NAME="${BDAG_RELEASE_NAME:-blockdag-pool-release-$STAMP}"
RELEASE_DIR="$RELEASE_ROOT/$RELEASE_NAME"
UNPACKED_DIR="$RELEASE_DIR/unpacked"
PACKAGE_DIR="$UNPACKED_DIR/$RELEASE_NAME"
ARCHIVES_DIR="$RELEASE_DIR/archives"
HELPERS_DIR="$RELEASE_DIR/helpers"
SHARE_DIR="$RELEASE_DIR/share-to-user"
PART_SIZE="${BDAG_RELEASE_PART_SIZE:-1800M}"
CHAIN_SOURCE="${BDAG_RELEASE_CHAIN_SOURCE:-$PROJECT_ROOT/data-restore/latest-hourly}"

LATEST_RELEASE="$(readlink -f "$RELEASE_ROOT/latest-blockdag-pool" 2>/dev/null || true)"
LATEST_UNPACKED=""
if [[ -n "$LATEST_RELEASE" && -d "$LATEST_RELEASE/unpacked" ]]; then
  LATEST_UNPACKED="$(find "$LATEST_RELEASE/unpacked" -mindepth 1 -maxdepth 1 -type d | head -n1 || true)"
fi
BASE_PACKAGE="${BDAG_RELEASE_BASE_PACKAGE:-${LATEST_UNPACKED:-$PROJECT_ROOT}}"

say() { printf '\n==> %s\n' "$*"; }

need_tool() {
  command -v "$1" >/dev/null 2>&1 || { echo "Required tool missing: $1" >&2; exit 1; }
}

write_reassemble_helpers() {
  local data_zip="$1"
  cat > "$HELPERS_DIR/reassemble-blockdag-chain-data-linux-mac.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
BASE="$data_zip"
cd "\$(dirname "\$0")"
echo "BlockDAG chain-data reassembly"
echo "================================"
mapfile -t parts < <(ls -1 "\$BASE".part-* 2>/dev/null | sort)
if (( \${#parts[@]} == 0 )); then
  echo "No chain-data part files found next to this script."
  exit 1
fi
cat "\${parts[@]}" > "\$BASE"
if [[ -f "\$BASE.sha256" ]] && command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c "\$BASE.sha256"
elif [[ -f "\$BASE.parts.sha256" ]] && command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c "\$BASE.parts.sha256"
fi
if command -v unzip >/dev/null 2>&1; then
  unzip -tq "\$BASE"
fi
echo "Created: \$BASE"
echo "Put this zip next to the stack installer folder before running ./install.sh."
EOF
  chmod +x "$HELPERS_DIR/reassemble-blockdag-chain-data-linux-mac.sh"

  cat > "$HELPERS_DIR/reassemble-blockdag-chain-data-windows.bat" <<EOF
@echo off
setlocal
set BASE=$data_zip
echo.
echo BlockDAG chain-data reassembly
echo =================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "\$parts=Get-ChildItem -Filter '%BASE%.part-*' | Sort-Object Name; if (\$parts.Count -eq 0) { Write-Error 'No part files found'; exit 1 }; \$out=[IO.File]::Create('%BASE%'); try { foreach (\$p in \$parts) { \$in=[IO.File]::OpenRead(\$p.FullName); try { \$in.CopyTo(\$out) } finally { \$in.Dispose() } } } finally { \$out.Dispose() }"
if errorlevel 1 goto failed
echo.
echo Created %BASE%
echo Put this zip next to the stack installer folder before running install.sh.
pause
exit /b 0
:failed
echo Reassembly failed. Make sure all part files are in this folder.
pause
exit /b 1
EOF
}

write_share_readme() {
  local stack_zip="$1" data_zip="$2"
  cat > "$SHARE_DIR/READ_ME_FIRST_BLOCKDAG_RELEASE.html" <<EOF
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BlockDAG Pool Release</title>
  <style>
    :root { color-scheme: dark; --bg:#101417; --panel:#171d21; --line:#334047; --text:#e9eef0; --muted:#a9b5bb; --green:#39d98a; --blue:#5cc8ff; --amber:#f0b35b; }
    body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    main { max-width:980px; margin:0 auto; padding:28px; }
    h1 { margin:0 0 8px; font-size:32px; }
    h2 { margin-top:28px; font-size:21px; }
    p, li { color:var(--muted); }
    code, pre { background:#0b0e10; border:1px solid var(--line); border-radius:6px; color:#f5f7f8; }
    code { padding:2px 5px; }
    pre { padding:14px; overflow:auto; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }
    .stack { border-left:4px solid var(--blue); }
    .data { border-left:4px solid var(--green); }
    .warn { border-left:4px solid var(--amber); }
    @media (max-width:720px){ .grid{grid-template-columns:1fr} h1{font-size:26px} }
  </style>
</head>
<body>
  <main>
    <h1>BlockDAG Pool Release</h1>
    <p>This release is split into an independent stack installer and an optional chain-data seed. You can share the stack without the data, or share the data separately when the recipient wants a faster initial sync.</p>
    <section class="grid">
      <div class="card stack"><strong>Stack installer</strong><p><code>$stack_zip</code> contains the pool, single-node runtime, dashboard, watchdog, tools, and image artifacts.</p></div>
      <div class="card data"><strong>Chain data</strong><p><code>$data_zip.part-*</code> are 1.8GB chunks. Reassemble them into <code>$data_zip</code> before install if fast sync is wanted.</p></div>
    </section>
    <h2>Install With Chain Data</h2>
    <pre><code>bash reassemble-blockdag-chain-data-linux-mac.sh
unzip $stack_zip
mv $data_zip $RELEASE_NAME/
cd $RELEASE_NAME
./install.sh</code></pre>
    <h2>Install Without Chain Data</h2>
    <pre><code>unzip $stack_zip
cd $RELEASE_NAME
./install.sh</code></pre>
    <div class="card warn"><strong>Note</strong><p>Without the data package, the node will sync from peers. With the data package, one seed is unpacked once for the backend node.</p></div>
  </main>
  <script type="application/json" id="agent-metadata">
  {
    "document_type": "release_share_instructions",
    "release_name": "$RELEASE_NAME",
    "stack_zip": "$stack_zip",
    "chain_data_zip": "$data_zip",
    "part_size": "$PART_SIZE",
    "data_is_optional": true
  }
  </script>
</body>
</html>
EOF
}

write_release_record() {
  local stack_zip="$1" data_zip="$2" data_source="$3"
  cat > "$RELEASE_DIR/RELEASE.html" <<EOF
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>$RELEASE_NAME</title></head>
<body>
  <h1>$RELEASE_NAME</h1>
  <p>Release folder: <code>$RELEASE_DIR</code></p>
  <h2>Share Folder</h2>
  <p><code>$SHARE_DIR</code></p>
  <h2>Artifacts</h2>
  <ul>
    <li><code>archives/$stack_zip</code> - independent stack installer.</li>
    <li><code>archives/$data_zip</code> - separate chain-data package.</li>
    <li><code>archives/$data_zip.part-*</code> - 1.8GB chain-data parts.</li>
  </ul>
  <script type="application/json" id="agent-metadata">
  {
    "document_type": "release_record",
    "release_name": "$RELEASE_NAME",
    "release_dir": "$RELEASE_DIR",
    "stack_zip": "$ARCHIVES_DIR/$stack_zip",
    "chain_data_zip": "$ARCHIVES_DIR/$data_zip",
    "chain_data_source": "$data_source",
    "part_size": "$PART_SIZE"
  }
  </script>
</body>
</html>
EOF
}

update_index() {
  mkdir -p "$RELEASE_ROOT"
  ln -sfn "$RELEASE_DIR" "$RELEASE_ROOT/latest-blockdag-pool"
  cat > "$RELEASE_ROOT/INDEX.html" <<EOF
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BlockDAG Releases</title></head>
<body>
  <h1>BlockDAG Releases</h1>
  <p>Latest: <a href="$RELEASE_NAME/RELEASE.html">$RELEASE_NAME</a></p>
  <p>Release root: <code>$RELEASE_ROOT</code></p>
  <script type="application/json" id="agent-metadata">
  {"document_type":"release_index","latest_release":"$RELEASE_NAME","release_root":"$RELEASE_ROOT"}
  </script>
</body>
</html>
EOF
}

need_tool rsync
need_tool zip
need_tool unzip
need_tool split
need_tool sha256sum

mkdir -p "$PACKAGE_DIR" "$ARCHIVES_DIR" "$HELPERS_DIR" "$SHARE_DIR"

say "Preparing package tree from $BASE_PACKAGE"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='data/' \
  --exclude='data-restore/' \
  --exclude='ops/runtime/' \
  --exclude='ops/runtime-*/' \
  --exclude='ops/__pycache__/' \
  --exclude='asic-pool/.env' \
  --exclude='.env' \
  --exclude='chain-data/' \
  "$BASE_PACKAGE"/ "$PACKAGE_DIR"/

say "Overlaying current production ops files"
rsync -a --delete --exclude='runtime/' --exclude='runtime-*/' --exclude='__pycache__/' \
  "$PROJECT_ROOT/ops"/ "$PACKAGE_DIR/ops"/
rsync -a "$PROJECT_ROOT/docker-compose.yml" "$PACKAGE_DIR"/
mkdir -p "$PACKAGE_DIR/asic-pool"
rsync -a "$PROJECT_ROOT/asic-pool/schema.sql" "$PACKAGE_DIR/asic-pool/schema.sql"
cp "$PROJECT_ROOT/ops/release-install.sh" "$PACKAGE_DIR/install.sh"
chmod +x "$PACKAGE_DIR/install.sh"

mkdir -p "$PACKAGE_DIR/chain-data"
cat > "$PACKAGE_DIR/README.html" <<EOF
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BlockDAG Pool Installer</title></head>
<body>
  <h1>BlockDAG Pool Installer</h1>
  <p>This stack package can be installed by itself. If a separate chain-data zip is present next to this folder, <code>install.sh</code> can use it to seed the backend node.</p>
  <pre><code>./install.sh</code></pre>
  <script type="application/json" id="agent-metadata">
  {"document_type":"installer_guide","release_name":"$RELEASE_NAME","chain_data_external":true}
  </script>
</body></html>
EOF

STACK_ZIP="$RELEASE_NAME-stack.zip"
DATA_ZIP="$RELEASE_NAME-chain-data.zip"

say "Creating independent stack zip"
(cd "$UNPACKED_DIR" && zip -qr "$ARCHIVES_DIR/$STACK_ZIP" "$RELEASE_NAME")
sha256sum "$ARCHIVES_DIR/$STACK_ZIP" > "$ARCHIVES_DIR/$STACK_ZIP.sha256"

if [[ -e "$CHAIN_SOURCE" ]]; then
  DATA_STAGE="$RELEASE_DIR/data-stage"
  rm -rf "$DATA_STAGE"
  mkdir -p "$DATA_STAGE/chain-data"
  say "Creating chain seed zip from $CHAIN_SOURCE"
  if [[ -d "$CHAIN_SOURCE" ]]; then
    (cd "$CHAIN_SOURCE" && zip -qr "$DATA_STAGE/chain-data/chain-data-seed.zip" .)
  else
    cp "$CHAIN_SOURCE" "$DATA_STAGE/chain-data/chain-data-seed.zip"
  fi
  (cd "$DATA_STAGE" && zip -qr -0 "$ARCHIVES_DIR/$DATA_ZIP" chain-data)
  sha256sum "$ARCHIVES_DIR/$DATA_ZIP" > "$ARCHIVES_DIR/$DATA_ZIP.sha256"
  say "Splitting chain data into $PART_SIZE parts"
  split -b "$PART_SIZE" -d -a 3 --numeric-suffixes=1 "$ARCHIVES_DIR/$DATA_ZIP" "$ARCHIVES_DIR/$DATA_ZIP.part-"
  sha256sum "$ARCHIVES_DIR/$DATA_ZIP".part-* > "$ARCHIVES_DIR/$DATA_ZIP.parts.sha256"
  rm -rf "$DATA_STAGE"
else
  echo "WARNING: chain source not found: $CHAIN_SOURCE" >&2
fi

write_reassemble_helpers "$DATA_ZIP"
write_share_readme "$STACK_ZIP" "$DATA_ZIP"
write_release_record "$STACK_ZIP" "$DATA_ZIP" "$CHAIN_SOURCE"

cp "$ARCHIVES_DIR/$STACK_ZIP" "$ARCHIVES_DIR/$STACK_ZIP.sha256" "$SHARE_DIR"/
if compgen -G "$ARCHIVES_DIR/$DATA_ZIP.part-*" >/dev/null; then
  cp "$ARCHIVES_DIR/$DATA_ZIP".part-* "$ARCHIVES_DIR/$DATA_ZIP.parts.sha256" "$ARCHIVES_DIR/$DATA_ZIP.sha256" "$SHARE_DIR"/
fi
cp "$HELPERS_DIR"/reassemble-blockdag-chain-data-* "$SHARE_DIR"/

update_index

PUBLISH_SCRIPT="${BDAG_RELEASE_PUBLISH_SCRIPT:-$RELEASE_ROOT/publish-latest-release.sh}"
if [[ "${BDAG_RELEASE_SKIP_DECENTRALIZED_PUBLISH:-0}" != "1" && -x "$PUBLISH_SCRIPT" ]]; then
  say "Publishing decentralized latest release pointers"
  nice -n 10 ionice -c2 -n7 "$PUBLISH_SCRIPT" "$RELEASE_DIR"
fi

say "Release complete"
echo "$RELEASE_DIR"
