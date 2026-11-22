"""Build a source release and static workbench into a directory outside this tree."""
import argparse
import gzip
import hashlib
import io
import shutil
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("destination", type=Path)
out = parser.parse_args().destination.resolve()
if out == root or root in out.parents:
    parser.error("use an output directory outside the source tree")
out.mkdir(parents=True, exist_ok=True)
directories = ["src", "include", "tests", "bench", "python", "examples", "web", "tools", ".github"]
files = [root / s for s in ["Makefile", "LICENSE", "README.md", "pyproject.toml", "setup.py",
                           "MANIFEST.in", ".gitignore"]]
for directory in directories:
    for path in (root / directory).rglob("*"):
        if path.is_file() and not path.is_symlink() and not any(
                part == "__pycache__" or part.endswith(".egg-info") for part in path.parts):
            files.append(path)

archive = out / "tailstate.tar.gz"
with archive.open("wb") as output, gzip.GzipFile(fileobj=output, filename="", mode="wb", mtime=0) as gz:
    with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(files):
            data = path.read_bytes()
            entry = tarfile.TarInfo("tailstate/" + path.relative_to(root).as_posix())
            entry.size = len(data)
            entry.mode = 0o644
            tar.addfile(entry, io.BytesIO(data))
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(out / "SHA256SUMS").write_text(digest + "  tailstate.tar.gz\n")
for src, dest in [
    ("web/index.html", "index.html"), ("bench/results.csv", "results.txt"),
    ("bench/pages-results.txt", "pages-results.txt"), ("bench/real/results.txt", "borg-results.txt"),
    ("bench/fleet.txt", "fleet-results.txt"), ("README.md", "README.md"),
]:
    shutil.copyfile(root / src, out / dest)
print(digest)
