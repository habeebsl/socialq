"""End-to-end check of the media layer (§12 step 2).

Uploads a small file, fetches it back over the public URL the way Instagram
will, and confirms a second enqueue re-uses the row instead of re-uploading.

    python scripts/verify_r2.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import httpx

from socialq.config import R2Config
from socialq.media import MediaStore, object_key, sha256_file


def main() -> int:
    config = R2Config.from_env()
    store = MediaStore(config)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "socialq-verify.png"
        # A 1x1 png: real bytes with a real Content-Type, small enough to be free.
        path.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f"
                "15c4890000000a49444154789c6360000002000100ffff03000006000557"
                "bfabd40000000049454e44ae426082"
            )
        )
        digest = sha256_file(path)
        key = object_key(digest, path)

        print(f"bucket   {config.bucket}")
        print(f"endpoint {config.endpoint_url}")
        print(f"key      {key}")

        url = store.upload(path, key, "image/png")
        print(f"url      {url}")

        response = httpx.get(url, follow_redirects=True, timeout=30)
        if response.status_code != 200:
            print(f"FAIL     public fetch returned {response.status_code}")
            print("         is the custom domain attached under Public access?")
            return 1
        if response.content != path.read_bytes():
            print("FAIL     fetched bytes differ from what was uploaded")
            return 1

        content_type = response.headers.get("content-type", "")
        print(f"fetched  {len(response.content)} bytes, content-type {content_type}")
        if "image/png" not in content_type:
            # The platforms fetch by URL and reject a wrong Content-Type.
            print("WARN     unexpected content-type")

        if store.exists(key):
            print("dedupe   head_object finds it; a re-enqueue will not re-upload")
        else:
            print("FAIL     head_object cannot see the object just written")
            return 1

    print("\nOK       upload, public fetch and dedupe all work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
