from ksch import Sch
s=Sch("LDO + LED indicator (spike)")
# --- power in: J1 -> C1 -> U1 (AMS1117-3.3) -> C2 -> +3V3 ; LED via R1 ; pushbutton to label
j=s.place('J1','Connector_Generic','Conn_01x02','USB 5V IN',40,60,0,fp='Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical',ref_off=(-7.62,-1.27),val_off=(-12.7,1.27))
u=s.place('U1','Regulator_Linear','AMS1117-3.3','AMS1117-3.3',80,57.15,0,fp='Package_TO_SOT_SMD:SOT-223-3_TabPin2',ref_off=(-5.08,-7.62),val_off=(-7.62,8.89))
c1=s.place('C1','Device','C','10u',62.23,66.04,0,fp='Capacitor_SMD:C_0805_2012Metric')
c2=s.place('C2','Device','C','22u',97.79,66.04,0,fp='Capacitor_SMD:C_0805_2012Metric')
r1=s.place('R1','Device','R','1k',120.65,57.15,0,fp='Resistor_SMD:R_0603_1608Metric')
d1=s.place('D1','Device','LED','PWR',120.65,71.12,90,fp='LED_SMD:LED_0603_1608Metric',ref_off=(3.81,-1.27),val_off=(3.81,1.27))
# nets
vin_y=u['1'][1] if u['1'][0]<u['2'][0] else None
print(u)
# VIN rail: J1.1 -> C1 top -> U1 input (pin1=GND? check) 
P=s.pin
VIN=57.15; GND=76.2
# J1 pins: 1 top(5V) 2 bottom(GND); J1 placed at x=40 pins on right side at x=45.08?
print(j)
# VIN rail
s.wire(P('J1','1'),(P('J1','1')[0],VIN),(P('U1','3')))
s.wire(P('C1','1'),(P('C1','1')[0],VIN)); s.junction(P('C1','1')[0],VIN)
x=P('C1','1')[0]; s.wire((x,VIN),(x,VIN-5.08)); s.power('VBUS',x,VIN-5.08)
s.wire((x,VIN-5.08),(x+5.08,VIN-5.08),(x+5.08,VIN-7.62)); s.power('PWR_FLAG',x+5.08,VIN-7.62); s.junction(x,VIN-5.08)
# 3V3 rail
s.wire(P('U1','2'),(P('R1','1')[0],VIN),P('R1','1'))
s.wire(P('C2','1'),(P('C2','1')[0],VIN)); s.junction(P('C2','1')[0],VIN)
x=P('C2','1')[0]; s.wire((x,VIN),(x,VIN-5.08)); s.power('+3V3',x,VIN-5.08)
# GND rail
for ref,p in [('J1','2'),('C1','2'),('U1','1'),('C2','2'),('D1','1')]:
    x,y=P(ref,p); s.wire((x,y),(x,GND))
s.wire((P('J1','2')[0],GND),(P('D1','1')[0],GND))
for ref,p in [('C1','2'),('U1','1'),('C2','2')]:
    s.junction(P(ref,p)[0],GND)
x=P('U1','1')[0]; s.power('GND',x,GND,0)
x2=P('C2','2')[0]; s.wire((x2,GND),(x2,GND+5.08)); s.power('PWR_FLAG',x2,GND+5.08,180)
# LED
s.wire(P('R1','2'),P('D1','2'))
s.text("Spike: hand-placed by agent, geometry by tool",40,40)
s.save('spike2.kicad_sch')
