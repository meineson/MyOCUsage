"""预览 v3：🍶 清酒瓶+小杯，精确对应Google emoji布局（杯在左下）"""
import math
from PIL import Image, ImageDraw

S = 88

def liquid_color(remaining_pct):
    if remaining_pct >= 60:
        return (60, 160, 220)
    elif remaining_pct >= 30:
        return (60, 180, 90)
    elif remaining_pct >= 10:
        return (220, 160, 40)
    else:
        return (220, 60, 60)

def render_icon(weekly_rem, fiveh_rem, angle=0):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── 德利瓶（右侧）──
    bcx = 52          # center x
    body_rx = 18      # 瓶身半径x
    body_ry = 24      # 瓶身半径y
    body_top = 30     # 瓶身上沿
    body_bot = body_top + body_ry * 2  # 瓶身下沿

    # 瓶颈
    neck_w = 10
    neck_h = 8
    neck_x0 = bcx - neck_w // 2
    neck_y0 = body_top - neck_h

    # 瓶口
    rim_w = 16
    rim_h = 4
    rim_x0 = bcx - rim_w // 2
    rim_y0 = neck_y0 - rim_h

    # 绘制瓶身轮廓
    draw.ellipse([bcx-body_rx, body_top, bcx+body_rx, body_bot],
                 fill=(235, 235, 235), outline=(180, 180, 180), width=2)
    draw.rectangle([neck_x0, neck_y0, neck_x0+neck_w, body_top],
                   fill=(235, 235, 235), outline=(180, 180, 180), width=2)
    draw.rounded_rectangle([rim_x0, rim_y0, rim_x0+rim_w, neck_y0],
                           radius=2, fill=(210, 210, 210), outline=(180, 180, 180))

    # 瓶身液体（从底往上填充weekly_rem%）
    if weekly_rem > 0:
        col = liquid_color(weekly_rem)
        liq_h = int(body_ry * 2 * weekly_rem / 100)
        liq_top = body_bot - liq_h
        mask = Image.new("L", (S, S), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse([bcx-body_rx, body_top, bcx+body_rx, body_bot], fill=255)
        md.rectangle([0, 0, S, liq_top], fill=0)  # 裁掉上半部

        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.ellipse([bcx-body_rx+2, body_top+2, bcx+body_rx-2, body_bot-2], fill=col)
        layer = Image.composite(layer, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask)
        img = Image.alpha_composite(img, layer)

        # 液面亮线
        if liq_top > body_top:
            light = (min(col[0]+60,255), min(col[1]+60,255), min(col[2]+60,255))
            for dx in range(-int(body_rx*0.85), int(body_rx*0.85)):
                nx = bcx + dx
                if ((nx-bcx)**2)/(body_rx**2) + ((liq_top-body_top-body_ry)**2)/(body_ry**2) <= 1:
                    draw.rectangle([nx, liq_top, nx, liq_top+1], fill=light)

    # 瓶身高光
    draw.ellipse([bcx-body_rx+5, body_top+8, bcx-body_rx+6, body_bot-10],
                 fill=(255, 255, 255, 50))

    # ── 小杯（左侧，靠近瓶底）──
    ccx = 24
    cup_y0 = body_bot - 28
    cup_w_top = 20
    cup_w_bot = 14
    cup_h = 18

    draw.polygon([
        (ccx-cup_w_top//2, cup_y0),
        (ccx+cup_w_top//2, cup_y0),
        (ccx+cup_w_bot//2, cup_y0+cup_h),
        (ccx-cup_w_bot//2, cup_y0+cup_h),
    ], fill=(235, 235, 235), outline=(180, 180, 180), width=2)
    draw.rectangle([ccx-cup_w_top//2-1, cup_y0-1, ccx+cup_w_top//2+1, cup_y0+1],
                   fill=(180, 180, 180))
    draw.rounded_rectangle([ccx-cup_w_bot//2-1, cup_y0+cup_h-2, ccx+cup_w_bot//2+1, cup_y0+cup_h+1],
                           radius=1, fill=(180, 180, 180))

    if fiveh_rem > 0:
        col = liquid_color(fiveh_rem)
        cup_liq_h = int(cup_h * fiveh_rem / 100)
        cup_liq_top = cup_y0 + cup_h - cup_liq_h
        for y in range(cup_liq_top, cup_y0 + cup_h):
            t = (y - cup_y0) / cup_h
            half_w = int(cup_w_top//2 - 1 - t * (cup_w_top - cup_w_bot)//2)
            draw.rectangle([ccx - half_w, y, ccx + half_w, y], fill=col)
        light = (min(col[0]+60,255), min(col[1]+60,255), min(col[2]+60,255))
        t = (cup_liq_top - cup_y0) / cup_h
        half_w = int(cup_w_top//2 - 1 - t * (cup_w_top - cup_w_bot)//2)
        draw.rectangle([ccx - half_w, cup_liq_top, ccx + half_w, cup_liq_top+1], fill=light)

    if angle != 0:
        img = img.rotate(angle, expand=True, fillcolor=(0,0,0,0), resample=Image.BICUBIC)
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w-side)//2, (h-side)//2, (w+side)//2, (h+side)//2))

    return img


import os
outdir = "/tmp/tokkuri_v3"
os.makedirs(outdir, exist_ok=True)

from PIL import ImageDraw as ID

variants = [(90,90), (70,50), (50,50), (30,30), (10,10),
            (80,20), (20,80), (100,100), (0,0)]

mosaic_44 = Image.new("RGBA", (len(variants)*60, 70), (240,240,240,255))
mosaic_z = Image.new("RGBA", (len(variants)*120, 140), (240,240,240,255))

for i, (wr, fr) in enumerate(variants):
    img88 = render_icon(wr, fr)
    img44 = img88.resize((44,44), Image.LANCZOS)
    img44.save(os.path.join(outdir, f"{wr}_{fr}.png"))
    mosaic_44.paste(img44, (i*60+8, 8), img44)
    ID.Draw(mosaic_44).text((i*60+4, 54), f"W{wr}C{fr}", fill=(0,0,0))
    zo = img88.resize((110,110), Image.NEAREST)
    mosaic_z.paste(zo, (i*120+5, 5), zo)
    ID.Draw(mosaic_z).text((i*120+10, 118), f"周{wr}%杯{fr}%", fill=(0,0,0))

mosaic_44.save(os.path.join(outdir, "actual_44.png"))
mosaic_z.save(os.path.join(outdir, "zoom.png"))
print(f"v3: {outdir}/")
open('/tmp/sake_emoji.png')

# Also save a single high-res for side by side
render_icon(55, 40).save('/tmp/tokkuri_v3/single.png')
render_icon(55, 40).resize((352,352), Image.NEAREST).save('/tmp/tokkuri_v3/single_4x.png')
