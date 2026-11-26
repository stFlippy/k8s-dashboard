"""Retry UID-bounded cleanup of one previously recorded lab benchmark run."""
import argparse
from pathlib import Path
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'benchmarks')]
from lab_connection import open_lab
from lab_dataset import LabDataset

parser=argparse.ArgumentParser(__doc__)
parser.add_argument('--state',type=Path,required=True)
parser.add_argument('--kubeconfig',type=Path)
args=parser.parse_args()
with tempfile.TemporaryDirectory(prefix='quaber-lab-cleanup-') as directory:
    store,cluster,_=open_lab(ROOT,directory,kubeconfig=args.kubeconfig)
    dataset=LabDataset.resume(store,cluster,args.state)
    dataset.cleanup()
    print('Owned benchmark namespaces verified absent.')
