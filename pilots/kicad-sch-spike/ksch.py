"""Tiny .kicad_sch writer: symbols from stock libs, wires, junctions, labels, power. Grid 1.27mm."""
import uuid, re, math
from pathlib import Path
LIB=Path('/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols')
U=lambda: str(uuid.uuid4())
G=1.27
def snap(v): return round(round(v/G)*G,4)

def _block(s,i):
    d=0;j=i
    while True:
        c=s[j]
        if c=='(':d+=1
        elif c==')':
            d-=1
            if d==0:return s[i:j+1]
        j+=1
_cache={}
def libsym(lib,sym):
    if (lib,sym) in _cache: return _cache[(lib,sym)]
    s=(LIB/f'{lib}.kicad_sym').read_text()
    i=s.find(f'(symbol "{sym}"\n'); assert i>=0,(lib,sym)
    body=_block(s,i)
    ext=re.search(r'\(extends "([^"]+)"\)',body)
    if ext:  # derived symbol: copy parent graphics, keep own props
        parent=libsym(lib,ext.group(1))[0]
        units=re.findall(r'\(symbol "%s_\d+_\d+".*?\n\t\t\)\n'%re.escape(ext.group(1)),parent,re.S)
        body=body.replace(ext.group(0),'')
        body=body[:-1]+''.join(u.replace(f'"{ext.group(1)}_',f'"{sym}_') for u in units)+')'
    body=body.replace(f'(symbol "{sym}"',f'(symbol "{lib}:{sym}"',1)
    pins={}
    for m in re.finditer(r'\(pin \w+ \w+\s+\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)\s+\(length ([-\d.]+)\).*?\(number "([^"]+)"',body,re.S):
        pins[m.group(5)]=(float(m.group(1)),float(m.group(2)))
    _cache[(lib,sym)]=(body,pins); return _cache[(lib,sym)]

class Sch:
    def __init__(self,title): self.root=U(); self.items=[]; self.libs={}; self.title=title; self.syms=[]
    def place(self,ref,lib,sym,value,x,y,rot=0,mirror=None,fp="",ref_off=(2.54,-1.27),val_off=(2.54,1.27),hide_ref=False):
        body,pins=libsym(lib,sym); self.libs[f'{lib}:{sym}']=body
        x,y=snap(x),snap(y)
        a=math.radians(rot)
        def xf(p):
            px,py=p
            if mirror=='x': py=-py
            if mirror=='y': px=-px
            rx=px*math.cos(a)-py*math.sin(a); ry=px*math.sin(a)+py*math.cos(a)
            return (snap(x+rx),snap(y-ry))
        self.pins_abs={**getattr(self,'pins_abs',{}),ref:{n:xf(p) for n,p in pins.items()}}
        m=f' (mirror {mirror})' if mirror else ''
        rx,ry=ref_off; vx,vy=val_off
        self.items.append(f'''(symbol (lib_id "{lib}:{sym}") (at {x} {y} {rot}){m} (unit 1) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) (uuid "{U()}")
  (property "Reference" "{ref}" (at {x+rx} {y+ry} 0) (effects (font (size 1.27 1.27)) (justify left){' (hide yes)' if hide_ref else ''}))
  (property "Value" "{value}" (at {x+vx} {y+vy} 0) (effects (font (size 1.27 1.27)) (justify left)))
  (property "Footprint" "{fp}" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Datasheet" "" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (instances (project "spike" (path "/{self.root}" (reference "{ref}") (unit 1))))
)''')
        return self.pins_abs[ref]
    def pin(self,ref,n): return self.pins_abs[ref][str(n)]
    def wire(self,*pts):
        pts=[(snap(a),snap(b)) for a,b in pts]
        for (x1,y1),(x2,y2) in zip(pts,pts[1:]):
            assert x1==x2 or y1==y2, f'non-orthogonal wire {pts}'
            self.items.append(f'(wire (pts (xy {x1} {y1}) (xy {x2} {y2})) (stroke (width 0) (type default)) (uuid "{U()}"))')
    def junction(self,x,y): self.items.append(f'(junction (at {snap(x)} {snap(y)}) (diameter 0) (color 0 0 0 0) (uuid "{U()}"))')
    def label(self,text,x,y,rot=0): self.items.append(f'(label "{text}" (at {snap(x)} {snap(y)} {rot}) (effects (font (size 1.27 1.27)) (justify left bottom)) (uuid "{U()}"))')
    def power(self,net,x,y,rot=0,ref='#PWR'):
        ref=f'{ref}{len([i for i in self.items if "#PWR" in i])+1:02d}'
        self.place(ref,'power',net,net,x,y,rot,val_off=(0,-2.54 if rot==0 else 2.54),hide_ref=True)
    def text(self,s,x,y): self.items.append(f'(text "{s}" (exclude_from_sim no) (at {snap(x)} {snap(y)} 0) (effects (font (size 1.5 1.5)) (justify left bottom)) (uuid "{U()}"))')
    def save(self,path):
        out=[f'(kicad_sch (version 20250114) (generator "pcbforge") (generator_version "0.1") (uuid "{self.root}") (paper "A4") (title_block (title "{self.title}"))',
             '(lib_symbols',*self.libs.values(),')',*self.items,'(sheet_instances (path "/" (page "1")))',')']
        Path(path).write_text('\n'.join(out))
