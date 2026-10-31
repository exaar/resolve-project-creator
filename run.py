import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))
from rpc.app import main

if __name__ == "__main__":
    main()
