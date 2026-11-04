import argparse
from .policy import ensure_image, build_image


def main():
    parser=argparse.ArgumentParser(description='LAN-first Docker image operations')
    sub=parser.add_subparsers(dest='action',required=True)
    pull=sub.add_parser('pull');pull.add_argument('image')
    build=sub.add_parser('build');build.add_argument('--tag','-t',required=True);build.add_argument('--file','-f',default='Dockerfile');build.add_argument('context',nargs='?',default='.')
    args=parser.parse_args()
    if args.action=='pull':print(ensure_image(args.image))
    else:build_image(args.tag,args.context,args.file)
