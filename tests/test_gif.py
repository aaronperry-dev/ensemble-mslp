"""The browser GIF encoder (site/gif.js) under Node, decoded with Pillow: frame count, timing, exact pixels when the
palette is exact, bounded error when it is quantised, and frame differencing keeping the file small."""
import json
import shutil
import subprocess

import numpy as np
import pytest

PIL = pytest.importorskip("PIL.Image")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def make(tmp_path, frames, w, h, many=False):
    out = tmp_path / "a.gif"
    r = subprocess.run(["node", str(ROOT / "tests/js/gif_make.js"), str(out), str(frames), str(w), str(h), "1" if many else "0"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    src = np.frombuffer((tmp_path / "a.gif.frames").read_bytes(), np.uint8).reshape(frames, h, w, 4)[..., :3]
    return out, src, json.loads(r.stdout)["bytes"]


def decode(path):
    im = PIL.open(path)
    frames, durs = [], []
    for i in range(im.n_frames):
        im.seek(i)
        frames.append(np.array(im.convert("RGB")))
        durs.append(im.info.get("duration"))
    return im, np.stack(frames), durs


def test_exact_palette_round_trips_every_frame(tmp_path):
    path, src, size = make(tmp_path, 12, 96, 60)
    im, got, durs = decode(path)
    assert im.size == (96, 60) and im.n_frames == 12 and im.info.get("loop") == 0
    assert durs[:-1] == [200] * 11 and durs[-1] == 1500
    # source colours are 8-bit; the encoder keeps 5 bits per channel, so compare after the same reduction
    red = lambda a: (a >> 3)
    assert np.array_equal(red(got), red(src)), int((red(got) != red(src)).sum())
    assert size < 12 * 96 * 60 * 3 / 8                               # frame differencing + LZW: far below raw


def test_many_colours_are_quantised_to_a_global_palette(tmp_path):
    path, src, _ = make(tmp_path, 4, 64, 48, many=True)
    im, got, _ = decode(path)
    assert got.shape == src.shape
    err = np.abs(got.astype(int) - src.astype(int)).mean()
    assert err < 30                                                  # 255 colours for a ramp of 10k+ colours: coarse but sane


def test_static_frames_cost_almost_nothing(tmp_path):
    out = tmp_path / "s.gif"
    js = """const G=require('%s/site/gif.js');const g=G.createGif(200,120);
    const px=new Uint8Array(200*120*4);for(let i=0;i<px.length;i+=4){px[i]=(i>>9)&255;px[i+1]=40;px[i+2]=90;px[i+3]=255}
    for(let f=0;f<30;f++)g.addFrame(px,100);require('fs').writeFileSync('%s',g.finish());""" % (ROOT, out)
    subprocess.run(["node", "-e", js], check=True, cwd=ROOT)
    one = out.stat().st_size
    im, got, _ = decode(out)
    assert im.n_frames == 30 and all(np.array_equal(got[0], g) for g in got)
    out1 = tmp_path / "one.gif"
    js1 = js.replace("f<30", "f<1").replace(str(out), str(out1))
    subprocess.run(["node", "-e", js1], check=True, cwd=ROOT)
    assert one - out1.stat().st_size < 30 * 40                       # each repeated frame is a few bytes


def test_comment_extension_describes_the_file_and_frames_still_decode(tmp_path):
    out = tmp_path / "c.gif"
    js = """const G=require('%s/site/gif.js');const g=G.createGif(40,30,{loop:0,comment:'GEFS | Ensemble mean MSLP (hPa). Init: 19 Nov 2026 00Z; F000 to F240 \u00b7 31 members. '+'x'.repeat(300)});
    const px=new Uint8Array(40*30*4).fill(200);for(let f=0;f<3;f++){px[f*4]=10;g.addFrame(px,100)}require('fs').writeFileSync('%s',g.finish());""" % (ROOT, out)
    subprocess.run(["node", "-e", js], check=True, cwd=ROOT)
    im, got, _ = decode(out)
    assert im.n_frames == 3 and im.info.get("loop") == 0
    c = im.info["comment"].decode()
    assert c.startswith("GEFS | Ensemble mean MSLP (hPa). Init: 19 Nov 2026 00Z; F000 to F240 ? 31 members.") and len(c) > 300   # >255 bytes spans sub-blocks; non-ASCII becomes '?'
