# -*- coding: utf-8 -*-
"""生成程序图标 app.ico / app.png。

设计：蓝紫渐变的方圆形底板 + 白色云朵（与程序内 sidebar logo 同一套造型）。
4 倍超采样后 LANCZOS 缩小，边缘干净；16px 下云朵轮廓仍然清楚。
"""
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 1024
K = 4
HERE = os.path.dirname(os.path.abspath(__file__))

C0 = (58, 92, 190)      # 左上 靛蓝（提亮一档，缩到 16px 也不糊）
C1 = (118, 70, 192)     # 右下 紫
RADIUS = 0.255

CLOUD_TOP = (255, 255, 255)     # 云顶：纯白
CLOUD_BOT = (236, 242, 252)     # 云底：只压一点点冷调，别压成灰


def diag_gradient(size, c0, c1):
    n = 256   # 先画小图再放大，渐变足够平滑，也快得多
    g = Image.new("RGB", (n, n))
    px = g.load()
    denom = 2.0 * (n - 1)
    for y in range(n):
        for x in range(n):
            t = (x + y) / denom
            px[x, y] = tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
    return g.resize((size, size), Image.BICUBIC)


def squircle_mask(size):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                        radius=int(size * RADIUS), fill=255)
    return m


def radial_glow(size, cx=0.28, cy=0.20, radius=0.78, peak=52):
    """左上角一团径向白光，给纯渐变加一点光源方向。"""
    n = 200
    g = Image.new("L", (n, n), 0)
    px = g.load()
    ox, oy, rr = cx * n, cy * n, radius * n
    for y in range(n):
        for x in range(n):
            d = ((x - ox) ** 2 + (y - oy) ** 2) ** 0.5 / rr
            if d < 1.0:
                px[x, y] = int((1.0 - d) ** 2 * peak)
    return g.resize((size, size), Image.BICUBIC)


def cloud_mask(size, grow=0, scale=1.0, flat=1.0):
    """云朵形状：三个圆 + 一条底线，合并成一张遮罩。

    grow  让整个形状内缩/外扩（做内侧亮边用）
    scale 整体缩放，用来控制云在图标里的占比
    flat  纵向压扁系数 —— 正圆堆出来的云会像一个圆坨，压扁才像云
    """
    u = size / 1024.0
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    cx0, cy0 = 512.0, 575.0          # 云的视觉中心，缩放围绕它
    # 中间一个大弧、两侧两个更小更低的弧，底部一条宽直线
    for cx, cy, r in ((546, 552, 156), (398, 640, 114), (662, 642, 102)):
        cx = cx0 + (cx - cx0) * scale
        cy = cy0 + (cy - cy0) * scale * flat
        rx = r * scale + grow
        ry = rx * flat
        d.ellipse([(cx - rx) * u, (cy - ry) * u, (cx + rx) * u, (cy + ry) * u], fill=255)
    x1 = cx0 + (298 - cx0) * scale
    x2 = cx0 + (744 - cx0) * scale
    y1 = cy0 + (610 - cy0) * scale * flat
    y2 = cy0 + (770 - cy0) * scale * flat
    d.rounded_rectangle([(x1 - grow) * u, (y1 - grow) * u,
                         (x2 + grow) * u, (y2 + grow) * u],
                        radius=78 * scale * u, fill=255)
    return m


def vertical_fill(size, top, bottom, mask):
    """云朵内部的垂直渐变（顶亮底暗，看起来才有体积）。"""
    grad = Image.new("RGB", (1, size))
    px = grad.load()
    for y in range(size):
        t = (y / max(1, size - 1)) ** 0.85
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    grad = grad.resize((size, size), Image.BICUBIC).convert("RGBA")
    grad.putalpha(mask)
    return grad


def top_gloss(size):
    """顶部一道很淡的高光，避免大色块发平。"""
    g = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(g)
    d.ellipse([-size * 0.35, -size * 0.95, size * 1.35, size * 0.52], fill=255)
    g = g.filter(ImageFilter.GaussianBlur(size * 0.05))
    return g


def build():
    size = S * K
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 1) 渐变底板 + 左上柔光
    base = diag_gradient(size, C0, C1).convert("RGBA")
    img.paste(base, (0, 0), squircle_mask(size))
    glow = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    glow.putalpha(radial_glow(size))
    img = Image.alpha_composite(img, glow)

    # 2) 云朵投影 + 一圈很淡的外发光（让云和底色之间有过渡，不发闷）
    cm = cloud_mask(size, scale=1.46, flat=0.72)
    sa = cm.filter(ImageFilter.GaussianBlur(size * 0.026)).point(lambda v: int(v * 0.32))
    shadow = Image.new("RGBA", (size, size), (10, 14, 28, 0))
    shadow.putalpha(sa)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    layer.alpha_composite(shadow, (0, int(size * 0.016)))
    img = Image.alpha_composite(img, layer)

    halo = cm.filter(ImageFilter.GaussianBlur(size * 0.032)).point(lambda v: int(v * 0.20))
    hl = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    hl.putalpha(halo)
    img = Image.alpha_composite(img, hl)

    # 3) 云朵本体：顶白 → 底淡蓝灰，不再是平涂一块白
    img = Image.alpha_composite(img, vertical_fill(size, CLOUD_TOP, CLOUD_BOT, cm))

    # 3b) 内侧亮边：云形减去「缩一圈的云形」，留一道玻璃边缘的高光
    ring = ImageChops.subtract(cm, cloud_mask(size, grow=-int(size * 0.010), scale=1.46, flat=0.72))
    ring = ring.filter(ImageFilter.GaussianBlur(size * 0.0035))
    rim = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    rim.putalpha(ring.point(lambda v: int(v * 0.55)))
    img = Image.alpha_composite(img, rim)

    # 4) 裁掉投影溢出
    img.putalpha(Image.composite(img.getchannel("A"),
                                 Image.new("L", (size, size), 0),
                                 squircle_mask(size)))

    out_img = img.resize((S, S), Image.LANCZOS)
    ico = os.path.join(HERE, "app.ico")
    out_img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (40, 40), (48, 48),
                             (64, 64), (128, 128), (256, 256)])
    png = os.path.join(HERE, "app.png")
    out_img.resize((256, 256), Image.LANCZOS).save(png)
    print("已生成", ico, os.path.getsize(ico), "字节")
    print("已生成", png, os.path.getsize(png), "字节")


if __name__ == "__main__":
    build()
