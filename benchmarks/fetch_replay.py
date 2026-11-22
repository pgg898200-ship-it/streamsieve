#!/usr/bin/env python3
"""Prepare hash-pinned ranked retrieval traces for benchmark replay.

The replay contains only document IDs ordered by the submissions' rank field.
The optional cache is local. No track corpus, text, or judgments are required.
"""

import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

REVISION = "8a896def007e3d657eb29f820ee3de98e2f32691"
ROOT = f"https://raw.githubusercontent.com/joaopalotti/trectools/{REVISION}/examples/robust03/runs"
RUNS = {
    "aplrob03a": "0efad3c9ccf250aa72ba02c3aa5c5fbda7e79754d80ba7a20d123a4e82e0d1c6",
    "UIUC03Rd1": "de6cc2178d9bbfa0400451a454bc620c64e3c37dc71b0dde940d2ab244e63099",
    "UAmsT03RDesc": "f231fd80ae71bcc6b195787fd9fbe1eaed2bbfd9ec280b23ec56eee7736f88cc",
    "SABIR03BASE": "83ff095095e8f8828a97452f5cbde46715db6b42212d01c9e2edef4d0acf8435",
}


HF_REVISION = "e78e61d1f8a8118f748efd7f4630aa008c240e19"
HF_ROOT = (
    "https://huggingface.co/datasets/GioiaZheng/msmarco-genqa-benchmark-runs"
    f"/resolve/{HF_REVISION}/raw/nfcorpus_video_query_ablation"
)
HF_RUNS = {
    "title_bm25": "b513c5f2c71eff11c96ca17415d284303b8a4158316fd4f26fca9a0777eebe4b",
    "title_bm25_ce": "c3b21ad0a93a3b0e89625dad3e5ba81235ba7da1890749caed45f40492196f3c",
    "description_bm25": "d50eea2413808cb2091d5da3e40c52aebee5d7d44d71558f9a635ccf3111dfbf",
    "description_bm25_ce": "d07eeff242e0c9a70a7df3bc5c98731211153e91f2f5d85434ca9ab4c1f37760",
    "title_plus_description_bm25": "0aa39b63e47f7b1b1ae1091b80a67ee3ac670cdb0f215617cbb59522533c18b4",
    "title_plus_description_bm25_ce": "5e7807fb66c5567a1aacebb1410fd80fd24b7e9a4a3e33113065b4d2f3603e9a",
}


def read_run(url, checksum, cache):
    name = url.rsplit("/", 1)[-1]
    path = cache / name if cache else None
    if path and path.exists():
        raw = path.read_bytes()
    else:
        with urlopen(url, timeout=60) as response:
            raw = response.read()
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError(f"Checksum mismatch for {name}")
    if path and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    rows = defaultdict(list)
    payload = gzip.decompress(raw) if name.endswith(".gz") else raw
    for line in payload.decode("ascii").splitlines():
        query, _, document, rank, _, _ = line.split()
        rows[query].append((int(rank), document))
    result = {}
    for query, hits in rows.items():
        ranks, documents = zip(*sorted(hits))
        if len(set(ranks)) != len(ranks) or len(set(documents)) != len(documents):
            raise ValueError(f"Ambiguous order for {query} in {name}")
        result[query] = documents
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", choices=("robust03", "nfcorpus-video"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--cache", type=Path, help="optional download cache directory")
    args = parser.parse_args()
    if args.dataset == "robust03":
        urls = {name: (f"{ROOT}/input.{name}.gz", sha) for name, sha in RUNS.items()}
        expected_topics, expected_depths = 100, [1000] * len(urls)
    else:
        urls = {name: (f"{HF_ROOT}/nfcorpus_video_{name}.tsv", sha) for name, sha in HF_RUNS.items()}
        expected_topics = 102
        expected_depths = [100 if name.endswith("_ce") else 1000 for name in urls]
    sources = [read_run(url, checksum, args.cache) for url, checksum in urls.values()]
    topics = sorted(sources[0], key=int if args.dataset == "robust03" else str)
    if len(topics) != expected_topics or any(set(source) != set(topics) for source in sources):
        raise ValueError("Unexpected topic set")
    queries = []
    for topic in topics:
        lists = [source[topic] for source in sources]
        if list(map(len, lists)) != expected_depths:
            raise ValueError(f"Unexpected list length for topic {topic}")
        queries.append({"id": topic, "lists": lists})
    replay = {"sources": list(urls), "queries": queries}
    args.output.write_text(json.dumps(replay, separators=(",", ":")) + "\n", encoding="ascii")


if __name__ == "__main__":
    main()
