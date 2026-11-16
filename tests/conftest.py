"""测试夹具：现场生成符合国内施工图习惯的 DXF 样例，不依赖外部文件。

生成 3 种图纸：
  * 建筑平面：双线墙 + 门窗块 + 轴网 + 图框
  * 结构平面：柱闭合多段线 + 双线梁 + 轴网
  * 不规范命名：中文/前后缀混用，验证映射鲁棒性
"""
from __future__ import annotations

import math
from pathlib import Path

import ezdxf
import pytest


def _setup(doc, units=4):
    doc.header["$INSUNITS"] = units


def make_arch(path: Path) -> Path:
    doc = ezdxf.new("R2018", setup=True)
    _setup(doc)
    msp = doc.modelspace()
    for name, color in [("A-WALL", 7), ("A-WALL-200", 7), ("A-DOOR", 3),
                        ("门窗", 3), ("AXIS", 1), ("A-DIM", 8),
                        ("图框", 8), ("FURN", 9)]:
        doc.layers.add(name, color=color)

    # 一个 6000x4800 的房间，墙厚 200：内外各一组双线
    # 外墙：外圈 (0,0)-(6200,5000)，内圈 (200,200)-(6000,4800)
    for lay, (x0, y0, x1, y1) in [
        ("A-WALL", (0, 0, 6200, 5000)),
        ("A-WALL", (200, 200, 6000, 4800)),
    ]:
        msp.add_line((x0, y0), (x1, y0), dxfattribs={"layer": lay})
        msp.add_line((x1, y0), (x1, y1), dxfattribs={"layer": lay})
        msp.add_line((x1, y1), (x0, y1), dxfattribs={"layer": lay})
        msp.add_line((x0, y1), (x0, y0), dxfattribs={"layer": lay})

    # 内墙 100 厚：y=2400 处一条水平双线（断成 3 段模拟图纸断线）
    for seg in [((200, 2350), (2000, 2350)), ((2000, 2350), (4000, 2350)),
                ((4000, 2350), (6000, 2350))]:
        msp.add_line(*seg, dxfattribs={"layer": "A-WALL-200"})
    msp.add_line((200, 2450), (6000, 2450), dxfattribs={"layer": "A-WALL-200"})

    # 门块
    door = doc.blocks.new(name="DOOR_900")
    door.add_line((0, 0), (900, 0))
    door.add_arc((0, 0), 900, 0, 90)
    msp.add_blockref("DOOR_900", (2000, 2350),
                     dxfattribs={"layer": "A-DOOR", "rotation": 0})
    # 窗：不规范图层名
    win = doc.blocks.new(name="窗C1515")
    win.add_line((0, 0), (1500, 0))
    win.add_line((0, 150), (1500, 150))
    msp.add_blockref("窗C1515", (3000, 0), dxfattribs={"layer": "门窗"})

    # 轴网：3 竖 2 横
    for i, x in enumerate([0, 3100, 6200]):
        msp.add_line((x, -800), (x, 5800), dxfattribs={"layer": "AXIS"})
        msp.add_text(str(i + 1), height=350,
                     dxfattribs={"layer": "AXIS"}).set_placement((x, 5900))
    for i, y in enumerate([0, 5000]):
        msp.add_line((-800, y), (7000, y), dxfattribs={"layer": "AXIS"})
        msp.add_text("AB"[i], height=350,
                     dxfattribs={"layer": "AXIS"}).set_placement((-1200, y))

    # 标注与图框（应被隔离/忽略）
    msp.add_line((0, -1500), (6200, -1500), dxfattribs={"layer": "A-DIM"})
    msp.add_text("平面图 1:100", height=400,
                 dxfattribs={"layer": "A-DIM"}).set_placement((0, -2000))
    for pts in [[(-3000, -3000), (10000, -3000), (10000, 9000), (-3000, 9000)]]:
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "图框"})
    # 家具（隔离层）
    msp.add_lwpolyline([(500, 500), (1500, 500), (1500, 1200), (500, 1200)],
                       close=True, dxfattribs={"layer": "FURN"})

    doc.saveas(path)
    return path


def make_struct(path: Path) -> Path:
    doc = ezdxf.new("R2018", setup=True)
    _setup(doc)
    msp = doc.modelspace()
    for n in ("S-COLU", "S-BEAM", "S-GRID", "S-SLAB", "平法标注"):
        doc.layers.add(n)

    for x in (0, 6000, 12000):
        for y in (0, 6000):
            msp.add_lwpolyline([(x - 300, y - 300), (x + 300, y - 300),
                                (x + 300, y + 300), (x - 300, y + 300)],
                               close=True, dxfattribs={"layer": "S-COLU"})
    # 双线梁 b=300
    for y in (0, 6000):
        msp.add_line((0, y - 150), (12000, y - 150), dxfattribs={"layer": "S-BEAM"})
        msp.add_line((0, y + 150), (12000, y + 150), dxfattribs={"layer": "S-BEAM"})
    # 板
    msp.add_lwpolyline([(0, 0), (12000, 0), (12000, 6000), (0, 6000)],
                       close=True, dxfattribs={"layer": "S-SLAB"})
    for i, x in enumerate([0, 6000, 12000]):
        msp.add_line((x, -1000), (x, 7000), dxfattribs={"layer": "S-GRID"})
        msp.add_text("KZ%d" % (i + 1), height=300,
                     dxfattribs={"layer": "S-GRID"}).set_placement((x, 7200))
    msp.add_text("KL1(2) 300x600", height=250,
                 dxfattribs={"layer": "平法标注"}).set_placement((3000, 300))
    doc.saveas(path)
    return path


def make_messy(path: Path) -> Path:
    """故意混乱：中文、空格、全角、$0$ 前缀、Xref 前缀、大小写混用。"""
    doc = ezdxf.new("R2018", setup=True)
    _setup(doc)
    msp = doc.modelspace()
    names = ["墙体 200", "ＷＡＬＬ", "$0$A-WALL", "图层1|柱子",
             " 门 ", "WINDOW - 窗", "轴线 ", "尺寸标注", "Defpoints"]
    for n in names:
        if n not in doc.layers:
            doc.layers.add(n)
    msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "墙体 200"})
    msp.add_line((0, 200), (5000, 200), dxfattribs={"layer": "墙体 200"})
    msp.add_line((0, 0), (0, 4000), dxfattribs={"layer": "ＷＡＬＬ"})
    msp.add_line((200, 0), (200, 4000), dxfattribs={"layer": "ＷＡＬＬ"})
    msp.add_lwpolyline([(6000, 0), (6600, 0), (6600, 600), (6000, 600)],
                       close=True, dxfattribs={"layer": "图层1|柱子"})
    msp.add_line((0, 5000), (8000, 5000), dxfattribs={"layer": "轴线 "})
    msp.add_text("1", height=300,
                 dxfattribs={"layer": "轴线 "}).set_placement((0, 5100))
    msp.add_text("说明文字", height=200,
                 dxfattribs={"layer": "尺寸标注"}).set_placement((0, -500))
    doc.saveas(path)
    return path


@pytest.fixture(scope="session")
def arch_dxf(tmp_path_factory):
    p = tmp_path_factory.mktemp("dwg") / "arch_plan.dxf"
    return make_arch(p)


@pytest.fixture(scope="session")
def struct_dxf(tmp_path_factory):
    p = tmp_path_factory.mktemp("dwg") / "struct_plan.dxf"
    return make_struct(p)


@pytest.fixture(scope="session")
def messy_dxf(tmp_path_factory):
    p = tmp_path_factory.mktemp("dwg") / "messy_plan.dxf"
    return make_messy(p)
