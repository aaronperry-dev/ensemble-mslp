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
    assert np.array_equal(got, src), int((got != src).any(axis=-1).sum())          # <= 255 colours: every pixel is reproduced exactly (24-bit, no reduction)
    assert size < 12 * 96 * 60 * 3 / 8                               # frame differencing + LZW: far below raw


def test_many_colours_are_quantised_to_a_global_palette(tmp_path):
    path, src, _ = make(tmp_path, 4, 64, 48, many=True)
    im, got, _ = decode(path)
    assert got.shape == src.shape
    err = np.abs(got.astype(int) - src.astype(int)).mean()
    assert err < 12                                                  # 255 colours for a ramp of 10k+ colours: coarse but sane


def test_static_frames_cost_almost_nothing(tmp_path):
    out = tmp_path / "s.gif"
    js = """const G=require('%s/site/gif.js');const g=G.createGif(200,120);
    const px=new Uint8Array(200*120*4);for(let i=0;i<px.length;i+=4){px[i]=(i>>9)&255;px[i+1]=40;px[i+2]=90;px[i+3]=255}
    for(let f=0;f<30;f++)g.addFrame(px,100);require('fs').writeFileSync('%s',g.finish());""" % (ROOT, out)
    subprocess.run(["node", "-e", js], check=True, cwd=ROOT)
    one = out.stat().st_size
    im, got, durs = decode(out)
    assert im.n_frames == 1 and durs == [3000]                       # 30 identical frames are stored once, shown for the summed delay
    out1 = tmp_path / "one.gif"
    js1 = js.replace("f<30", "f<1").replace(str(out), str(out1))
    subprocess.run(["node", "-e", js1], check=True, cwd=ROOT)
    assert one - out1.stat().st_size < 8                             # repeated frames cost nothing at all


def test_comment_extension_describes_the_file_and_frames_still_decode(tmp_path):
    out = tmp_path / "c.gif"
    js = """const G=require('%s/site/gif.js');const g=G.createGif(40,30,{loop:0,comment:'GEFS | Ensemble mean MSLP (hPa). Init: 19 Nov 2026 00Z; F000 to F240 \u00b7 31 members. '+'x'.repeat(300)});
    const px=new Uint8Array(40*30*4).fill(200);for(let f=0;f<3;f++){px[f*4]=10;g.addFrame(px,100)}require('fs').writeFileSync('%s',g.finish());""" % (ROOT, out)
    subprocess.run(["node", "-e", js], check=True, cwd=ROOT)
    im, got, _ = decode(out)
    assert im.n_frames == 3 and im.info.get("loop") == 0
    c = im.info["comment"].decode()
    assert c.startswith("GEFS | Ensemble mean MSLP (hPa). Init: 19 Nov 2026 00Z; F000 to F240 ? 31 members.") and len(c) > 300   # >255 bytes spans sub-blocks; non-ASCII becomes '?'


JS_FLAT = """
const G = require('%s/site/gif.js');
const W = 160, H = 100, N = 10;
// flat class colours (distinct 8-bit values that a 5-bit palette would merge), a sprinkling of anti-aliased greys, a moving blob
const base = [[34, 94, 168], [35, 94, 168], [33, 147, 193], [65, 182, 196], [127, 205, 187], [255, 196, 0]];
const g = G.createGif(W, H, { loop: 0 }), frames = [];
let seed = 99; const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
for (let f = 0; f < N; f++) {
  const px = new Uint8Array(W * H * 4);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const k = (y * W + x) * 4; let c = base[Math.floor((x + 3 * y) / 40) %% base.length];
    const d = Math.hypot(x - (20 + f * 12), y - 50); if (d < 18) c = base[(Math.floor(d / 6) + f) %% base.length];
    if ((x * 7 + y * 13) %% 11 === 0) { const v = 60 + Math.floor(rnd() * 3) * 70; c = [v, v, v]; }          // 3 grey levels of "anti-aliasing"
    if (x === W - 1 - f && y > 5) c = [250 - f * 3, 20 + f, 30];                                           // a thin moving line, a new colour each frame
    px[k] = c[0]; px[k + 1] = c[1]; px[k + 2] = c[2]; px[k + 3] = 255;
  }
  frames.push(Buffer.from(px)); g.addFrame(px, 100);
}
const out = {};
for (const [name, o] of Object.entries(%s)) { const b = g.finish(o); require('fs').writeFileSync('%s/' + name + '.gif', b); out[name] = { bytes: b.length, stats: g.stats }; }
require('fs').writeFileSync('%s/frames.raw', Buffer.concat(frames));
console.log(JSON.stringify(out));
"""


def flat_run(tmp_path, variants):
    js = JS_FLAT % (ROOT, json.dumps(variants), tmp_path, tmp_path)
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    src = np.frombuffer((tmp_path / "frames.raw").read_bytes(), np.uint8).reshape(10, 100, 160, 4)[..., :3]
    return json.loads(r.stdout), src


def test_flat_class_colours_are_never_shifted_even_with_many_other_colours(tmp_path):
    """Class colours differing by one level survive; with > 255 colours the big flat areas stay exact and only rare blends are approximated."""
    js = """const G=require('%s/site/gif.js');const g=G.createGif(120,80);
    for(let f=0;f<3;f++){const px=new Uint8Array(120*80*4);for(let y=0;y<80;y++)for(let x=0;x<120;x++){const k=(y*120+x)*4;
      let c=y<40?[34,94,168]:[35,94,168];                                   // two flat colours 1 level apart (a 5-bit palette merges them)
      if(x<40&&y>=70)c=[(x*6+f)%%256,(y*9)%%256,(x*y+f*5)%%256];            // a small patch of > 255 distinct colours
      px[k]=c[0];px[k+1]=c[1];px[k+2]=c[2];px[k+3]=255}g.addFrame(px,100)}
    require('fs').writeFileSync('%s',g.finish());console.log(JSON.stringify(g.stats))""" % (ROOT, tmp_path / "q.gif")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["exact"] is False
    _, got, _ = decode(tmp_path / "q.gif")
    assert (got[:, :40] == [34, 94, 168]).all() and (got[:, 40:70] == [35, 94, 168]).all()      # exact, not merged
    patch = got[:, 70:, :40].reshape(-1, 3)
    assert len(np.unique(patch, axis=0)) < 256


def test_lossless_variants_decode_to_the_same_pixels_and_identical_frames_merge(tmp_path):
    st, src = flat_run(tmp_path, {"a": {}, "b": {"maxColors": 255, "tolerance": 0}})
    assert st["a"]["bytes"] == st["b"]["bytes"] and st["a"]["stats"]["exact"] is True
    _, got, durs = decode(tmp_path / "a.gif")
    assert np.array_equal(got, src)
    # two identical frames in a row become one frame with the summed delay
    js = """const G=require('%s/site/gif.js');const g=G.createGif(30,20);const a=new Uint8Array(30*20*4).fill(120),b=new Uint8Array(30*20*4).fill(40);
    for (const p of [a,a,b,b,b,a]) g.addFrame(p,100);require('fs').writeFileSync('%s',g.finish());console.log(JSON.stringify(g.stats))""" % (ROOT, tmp_path / "m.gif")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    st2 = json.loads(r.stdout)
    assert st2["frames"] == 3 and st2["written"] == 6
    im, got2, d2 = decode(tmp_path / "m.gif")
    assert im.n_frames == 3 and d2 == [200, 300, 100] and (got2[0] == 120).all() and (got2[1] == 40).all() and (got2[2] == 120).all()


def test_fewer_colours_and_tolerance_make_smaller_files_that_still_decode(tmp_path):
    st, src = flat_run(tmp_path, {"full": {}, "c64": {"maxColors": 64}, "lossy": {"maxColors": 64, "tolerance": 12}})
    assert st["c64"]["stats"]["colors"] <= 64
    assert st["lossy"]["bytes"] <= st["c64"]["bytes"] <= st["full"]["bytes"]
    for name in ("full", "c64", "lossy"):
        im, got, _ = decode(tmp_path / f"{name}.gif")
        assert got.shape == src.shape and im.n_frames == 10
    _, got, _ = decode(tmp_path / "lossy.gif")
    assert np.abs(got.astype(int) - src.astype(int)).mean() < 12                      # bounded error, no drift over the frames
    assert np.abs(got[-1].astype(int) - src[-1].astype(int)).mean() < 12


def test_a_run_extending_pixel_keeps_the_image_identical(tmp_path):
    """Unchanged pixels inside a changed box are written as their own colour when that lengthens a run: the picture is still exact."""
    js = """const G=require('%s/site/gif.js');const W=64,H=40,g=G.createGif(W,H),fr=[];
    for(let f=0;f<6;f++){const px=new Uint8Array(W*H*4);for(let y=0;y<H;y++)for(let x=0;x<W;x++){const k=(y*W+x)*4;
      const on=((x+f*2)%%8)<5||(y%%4===0)?1:0;                                  // stripes that move a little: changed pixels are mixed with unchanged ones
      const c=on?[200,30,30]:[20,20,90];px[k]=c[0];px[k+1]=c[1];px[k+2]=c[2];px[k+3]=255}fr.push(Buffer.from(px));g.addFrame(px,100)}
    require('fs').writeFileSync('%s/r.gif',g.finish());require('fs').writeFileSync('%s/r.raw',Buffer.concat(fr))""" % (ROOT, tmp_path, tmp_path)
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    src = np.frombuffer((tmp_path / "r.raw").read_bytes(), np.uint8).reshape(6, 40, 64, 4)[..., :3]
    _, got, _ = decode(tmp_path / "r.gif")
    assert np.array_equal(got, src)
