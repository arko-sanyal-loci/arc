"""Frozen LOCI-context adapter used by the 2026-11-20 test.

The TF-IDF implementation is loaded from the pinned LOCI Engine Git commit
below. Set LOCI_ENGINE_CHECKOUT to a checkout containing that commit when
reproducing outside the original workspace.
"""
from functools import lru_cache
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

COMPRESSOR_REF = "bbd50360a868ea4e97e8888d4cf46786b6652620"

@lru_cache(maxsize=1)
def compressor():
    checkout = Path(os.environ.get("LOCI_ENGINE_CHECKOUT", str(Path(__file__).resolve().parents[5] / "loci-canonical")))
    source = subprocess.check_output(
        ["git", "-C", str(checkout), "show", f"{COMPRESSOR_REF}:loci_engine/compressor.py"], timeout=5)
    module = ModuleType("benchmark_loci_compressor")
    sys.modules[module.__name__] = module
    exec(compile(source, f"{COMPRESSOR_REF}:loci_engine/compressor.py", "exec"), module.__dict__)
    return module

def compact(messages, threshold=6500):
    turns = [i for i,m in enumerate(messages) if m["role"] != "system"]
    original = sum(len(m["content"]) for m in messages)
    if original <= threshold or len(turns) <= 5: return messages
    cutoff = turns[-5]
    older = [m for m in messages[:cutoff] if m["role"] != "system"]
    question = next((m["content"] for m in reversed(messages) if m["role"]=="user"),"")
    digest = compressor().compress([f'{m["role"]}: {m["content"]}' for m in older],query=question,max_tokens=420)
    if not digest.text or digest.compressed_tokens > 600: return messages
    result=[]
    for i,m in enumerate(messages):
        if i==cutoff:
            result.append({"role":"assistant","content":"Selected excerpts from prior turns, included as historical evidence.\n"+digest.text})
        if i>=cutoff or m["role"]=="system": result.append(m)
    return result if sum(len(m["content"]) for m in result)<original else messages
