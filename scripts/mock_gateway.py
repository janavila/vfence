#!/usr/bin/env python3
import argparse,itertools,sys,time
MESSAGES=["V1|COL01|POS|-31.306119|-54.063935|SEGURO|12|0.90|{sequence}","V1|COL01|POS|-31.306120|-54.063930|ATENCAO|11|1.10|{sequence}","V1|COL01|POS|-31.306130|-54.063920|CRITICO|10|1.20|{sequence}","V1|COL01|POS|-31.306140|-54.063910|FORA|9|1.50|{sequence}"]
def main():
    parser=argparse.ArgumentParser(description="Simulador de gateway VFence");parser.add_argument('--interval',type=float,default=3);args=parser.parse_args()
    try:
        for sequence,template in enumerate(itertools.cycle(MESSAGES),1): print(template.format(sequence=sequence),flush=True);time.sleep(args.interval)
    except KeyboardInterrupt: sys.exit(0)
if __name__=='__main__': main()
