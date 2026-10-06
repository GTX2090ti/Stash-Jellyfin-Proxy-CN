"""Verify every shipped module parses under the container's Python (3.11).

Local runs on 3.13 and the image is 3.11, so a 3.12+ only syntax feature
would compile here and break the build. Mirrors the pre-deploy gate
described in the deploy skill.
"""
import ast
import pathlib
import sys

bad = 0
checked = 0
for p in sorted(pathlib.Path("stash_jellyfin_proxy").rglob("*.py")):
    checked += 1
    try:
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p), feature_version=(3, 11))
    except SyntaxError as e:
        print("FAIL", p, e)
        bad += 1

print("files checked      :", checked)
print("py3.11 syntax      :", "ALL OK" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
