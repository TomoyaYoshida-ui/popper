"""Export tracked, present source files only; never export credentials or runtime data."""
import argparse
import subprocess
import tarfile
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    paths=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
    additions=['scripts/export_transfer_validation.py','evaluation/validate_transfer_environment.py',
               'evaluation/verify_transfer_pilot.py','scripts/Dockerfile.transfer']
    args.output.parent.mkdir(parents=True,exist_ok=True)
    count=0
    with tarfile.open(args.output,'w:gz') as archive:
        for name in sorted(set(paths+additions)):
            if not name: continue
            parts=Path(name).parts
            if any(p in {'.git','.acceptance','.env','__pycache__','node_modules','runs'} for p in parts): continue
            source=root/name
            if source.is_file() and source.suffix.lower() not in {'.sqlite','.db','.zip','.pem','.key'}:
                archive.add(source,arcname=name,recursive=False); count+=1
    print(f'Exported {count} source files')


if __name__=='__main__': main()
