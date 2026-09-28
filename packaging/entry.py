"""PyInstaller 入口：ESG-XK.exe（GUI）与 ESG-XK.exe --worker（Processing Worker）共用。"""
import sys

from esgxk.app_main import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
