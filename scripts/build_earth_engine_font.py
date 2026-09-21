#!/usr/bin/env python3
"""Build static vector glyphs; requires the optional Matplotlib chart environment."""
import json
from pathlib import Path

import matplotlib
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextPath, TextToPath
from shapely.geometry import Polygon


def main():
    root=Path(__file__).resolve().parent/'earth_engine_assets'
    root.mkdir(exist_ok=True)
    fonts={}
    for weight in ('normal','bold'):
        prop=FontProperties(family='DejaVu Sans',weight=weight,size=100)
        glyphs={}
        for char in ''.join(chr(n) for n in range(32,127))+'©°':
            width=TextToPath().get_text_width_height_descent(char,prop,False)[0]/100
            rings=TextPath((0,0),char,prop=prop,size=100).to_polygons() if char!=' ' else []
            polygons=[Polygon(ring) for ring in rings]
            outers=[i for i,p in enumerate(polygons)
                    if sum(q.contains(p.representative_point()) for j,q in enumerate(polygons)
                           if i!=j and q.area>p.area)%2==0]
            grouped=[]
            for i in outers:
                holes=[j for j,p in enumerate(polygons) if j not in outers
                       and polygons[i].contains(p.representative_point())
                       and not any(polygons[k].area<polygons[i].area and polygons[k].contains(p.representative_point())
                                   for k in outers if k!=i)]
                grouped.append([[[round(float(x)/100,5),round(float(y)/100,5)] for x,y in rings[j]]
                                for j in [i,*holes]])
            glyphs[char]={'advance':round(width,5),'polygons':grouped}
        fonts[weight]=glyphs
    (root/'font.json').write_text(json.dumps(fonts,separators=(',',':'))+'\n')
    license_text=(Path(matplotlib.get_data_path())/'fonts/ttf/LICENSE_DEJAVU').read_text()
    (root/'LICENSE_DEJAVU').write_text('\n'.join(line.rstrip() for line in license_text.splitlines())+'\n')


if __name__=='__main__':
    main()
