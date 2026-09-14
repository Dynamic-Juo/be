"""Isolated Mac mini probe. Explicit images only; never changes production networks."""
import argparse
import json
import subprocess
import time
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--docker', required=True)
    parser.add_argument('--probe-image', required=True)
    parser.add_argument('--proxy-image', required=True)
    parser.add_argument('--private-target', required=True)
    args = parser.parse_args()
    prefix = 'chamsae-egress-check-' + uuid.uuid4().hex[:8]
    private, outbound, proxy = prefix + '-private', prefix + '-out', prefix + '-proxy'
    created = []
    def run(arguments, **kwargs):
        return subprocess.run([args.docker, *arguments], check=True, **kwargs)
    try:
        for name, flags in [(private, ['--internal']), (outbound, [])]:
            run(['network', 'create', *flags, '--label', 'purpose=chamsae-isolation-check', name], stdout=subprocess.DEVNULL)
            created.append(name)
        run(['create', '--name', proxy, '--network', outbound, '--add-host', 'api.deepseek.com:' + args.private_target,
             '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '32',
             '--memory', '128m', '--cpus', '0.25', '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777', args.proxy_image], stdout=subprocess.DEVNULL)
        run(['network', 'connect', '--alias', 'chamsae-egress', private, proxy])
        run(['start', proxy], stdout=subprocess.DEVNULL)
        time.sleep(2)
        networks = json.loads(run(['inspect', '--format', '{{json .NetworkSettings.Networks}}', proxy], capture_output=True, text=True).stdout)
        proxy_ip = networks[private]['IPAddress']
        script = '''import json,socket,sys,urllib.request,urllib.error
try: socket.getaddrinfo('chamsae-egress',3128); print('proxy_dns=resolved')
except OSError: print('proxy_dns=unavailable; test uses inspected private address')
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k): return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://'+sys.argv[1]+':3128','http':'http://'+sys.argv[1]+':3128'}),NoRedirect())
for label,url,blocked in [('allowed-provider','https://openapi.naver.com/v1/search/news.json',False),('unlisted-domain','https://example.com/',True),('private-literal','https://'+sys.argv[2]+'/',True),('allowed-name-private-dns','https://api.deepseek.com/',True)]:
 try:
  with opener.open(url,timeout=8) as r: state='upstream-http-'+str(r.status)
 except urllib.error.HTTPError as e: state='upstream-http-'+str(e.code)
 except urllib.error.URLError as e: state='proxy-denied' if '403' in str(e.reason) else type(e.reason).__name__+':'+str(e.reason)
 print(json.dumps({'case':label,'outcome':state}),flush=True)
 assert (state=='proxy-denied') if blocked else state.startswith('upstream-http-'),(label,state)
'''
        run(['run', '--rm', '--network', private, '--user', '10001:10001', '--read-only', '--cap-drop', 'ALL',
             '--security-opt', 'no-new-privileges', '--memory', '96m', '--pids-limit', '32', args.probe_image,
             'python', '-c', script, proxy_ip, args.private_target])
    finally:
        subprocess.run([args.docker, 'rm', '-f', proxy], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name in reversed(created):
            run(['network', 'rm', name], stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
