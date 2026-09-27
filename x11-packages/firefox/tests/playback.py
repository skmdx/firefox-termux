#!/usr/bin/env python3
"""Run real Firefox video playback on Termux; keep logs and frame evidence."""
import argparse
import functools
import http.server
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import threading
import time

PAGE = b'''<!doctype html><meta charset="utf-8"><title>MediaCodec playback</title>
<style>body{margin:0;background:#222;color:white}video{width:960px}</style>
<video id="v" muted autoplay></video><pre id="status"></pre>
<script>
const v=document.querySelector('video'), events=[];
function snapshot() {const q=v.getVideoPlaybackQuality();return {time:v.currentTime,width:v.videoWidth,height:v.videoHeight,
  decoded:v.mozDecodedFrames,presented:v.mozPresentedFrames,
  quality:{total:q.totalVideoFrames,dropped:q.droppedVideoFrames},
  error:v.error && {code:v.error.code,message:v.error.message}};}
function event(name) {return new Promise((resolve,reject)=>{
  v.addEventListener(name,resolve,{once:true});
  v.addEventListener('error',()=>reject(new Error(JSON.stringify(snapshot()))),{once:true});
});}
async function run() {
  const ended=event('ended'); v.src='/video.mp4'; await v.play();
  await fetch('/checkpoint/start',{method:'POST',body:'{}'});
  await ended; events.push({stage:'ended',...snapshot()});
  await fetch('/checkpoint/end',{method:'POST',body:'{}'});
  const sought=event('seeked'); v.currentTime=2; await sought;
  events.push({stage:'seeked',...snapshot()}); await v.play();
  await new Promise(r=>setTimeout(r,2000)); v.pause();
  events.push({stage:'replayed',...snapshot()});
  for(let i=0;i<SEEK_COUNT;i++) {
    const sought=event('seeked'); v.currentTime=i%2 ? 2 : 8; await sought;
    await v.play(); await new Promise(r=>setTimeout(r,250)); v.pause();
    events.push({stage:'repeated-seek',...snapshot()});
  }
  const capture=event('seeked');
  const frameShown=new Promise(resolve=>v.requestVideoFrameCallback((now,metadata)=>resolve(metadata.mediaTime)));
  v.currentTime=CAPTURE_TIME; await capture; const mediaTime=await frameShown;
  const c=document.createElement('canvas'); c.width=320;c.height=180;
  c.getContext('2d').drawImage(v,0,0,c.width,c.height);
  const pixels=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
  let lo=255,hi=0; for(let i=0;i<pixels.length;i++)if(i%4!=3){lo=Math.min(lo,pixels[i]);hi=Math.max(hi,pixels[i]);}
  events.push({stage:'capture',...snapshot(),mediaTime,pixelRange:hi-lo,frame:c.toDataURL()});
  document.querySelector('#status').textContent=JSON.stringify(events,null,2);
  await fetch('/result',{method:'POST',body:JSON.stringify({events})});
}
run().catch(error=>fetch('/result',{method:'POST',body:JSON.stringify({error:String(error),events})}));
</script>'''


def stop(process):
    # Android does not delegate writable cgroups to a Termux UID.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'For GPU tests, set GALLIUM_DRIVER=zink, MESA_LOADER_DRIVER_OVERRIDE=zink, '
        'and VK_DRIVER_FILES to the device Vulkan ICD. --gpu enables hardware '
        'WebRender and X11 EGL in the isolated test profile. Fixtures must be '
        'MP4 with nb_frames metadata and longer than the seek/capture positions.'))
    parser.add_argument('--firefox', type=Path, required=True)
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--software', action='store_true')
    parser.add_argument('--cpu-buffer', action='store_true')
    parser.add_argument('--gpu', action='store_true')
    parser.add_argument('--expect-cpu', action='store_true', help='Expect automatic CPU output without changing the surface preference')
    parser.add_argument('--display', default=':5')
    parser.add_argument('--capture-time', type=float, default=4)
    parser.add_argument('--seek-count', type=int, default=0)
    args = parser.parse_args()
    page = PAGE.replace(b'CAPTURE_TIME', str(args.capture_time).encode())
    page = page.replace(b'SEEK_COUNT', str(args.seek_count).encode())
    args.out.mkdir(parents=True, exist_ok=False)
    metadata = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,nb_frames,duration', '-of', 'json',
        str(args.video)]))['streams'][0]
    expected_frames = int(metadata['nb_frames'])
    result = {'video': metadata}
    done = threading.Event()
    browser = None
    ticks = os.sysconf('SC_CLK_TCK')

    def usage():
        cpu = 0
        io = {'read_bytes': 0, 'write_bytes': 0}
        io_available = True
        pids = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) != browser.pid:
                    continue
                pids.append(int(entry.name))
                cpu += int(fields[11]) + int(fields[12])
                try:
                    counters = dict(line.split(':') for line in (entry/'io').read_text().splitlines())
                    for key in io:
                        io[key] += int(counters[key])
                except PermissionError:
                    io_available = False
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                continue
        return {'cpu_seconds': cpu/ticks, 'monotonic': time.monotonic(),
                'io': io if io_available else None, 'pids': pids}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_GET(self):
            if self.path == '/':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            else:
                self.path = '/' + args.video.name
                super().do_GET()

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.startswith('/checkpoint/'):
                result[self.path.rsplit('/', 1)[-1]] = usage()
            else:
                result.update(data)
            self.send_response(200)
            self.end_headers()
            if self.path == '/result':
                done.set()

    handler = functools.partial(Handler, directory=str(args.video.parent))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = args.out / 'profile'
    profile.mkdir()
    prefs = {'media.ffmpeg.mediacodec.enabled': not args.software,
             'media.ffmpeg.mediacodec.surface.enabled': not args.cpu_buffer,
             'media.autoplay.default': 0, 'browser.shell.checkDefaultBrowser': False,
             'browser.startup.homepage_override.mstone': 'ignore',
             'browser.aboutwelcome.enabled': False, 'browser.startup.page': 0,
             'datareporting.policy.firstRunURL': ''}
    if args.gpu:
        prefs.update({'gfx.webrender.all': True, 'gfx.webrender.software': False,
                      'layers.acceleration.force-enabled': True, 'gfx.x11-egl.force-enabled': True})
    (profile/'user.js').write_text(''.join(f'user_pref({json.dumps(k)}, {json.dumps(v)});\n'
                                          for k,v in prefs.items()))
    env = dict(os.environ, DISPLAY=args.display, MOZ_LOG='FFmpegVideo:5,PlatformDecoderModule:4,TermuxMediaCodec:5,Dmabuf:4',
               MOZ_LOG_FILE=str(args.out/'decode.log'))
    x11 = None
    try:
        if subprocess.run(['xdpyinfo', '-display', args.display], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode:
            x11 = subprocess.Popen(['termux-x11', args.display, '-ac'], env=env,
                                   start_new_session=True, stdout=(args.out/'x11.log').open('w'),
                                   stderr=subprocess.STDOUT)
            for _ in range(50):
                if subprocess.run(['xdpyinfo', '-display', args.display], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0:
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('X11 server did not become ready')
        browser = subprocess.Popen([str(args.firefox), '--no-remote', '--profile', str(profile),
                                    f'http://127.0.0.1:{server.server_port}/'],
                                   env=env, start_new_session=True,
                                   stdout=(args.out/'browser.log').open('w'), stderr=subprocess.STDOUT)
        result['completed'] = done.wait(float(metadata['duration']) + 55)
        subprocess.run(['import', '-display', args.display, '-window', 'root',
                        str(args.out/'screen.png')], timeout=15, check=True)
    finally:
        if browser:
            stop(browser)
        if x11:
            stop(x11)
        server.shutdown()
        result['cgroup'] = 'unavailable: Android does not delegate cgroups to Termux'
        logs = ''.join(p.read_text() for p in args.out.glob('decode.log*'))
        result['mediacodec'] = 'MediaCodec FFmpeg init successful' in logs
        result['software_init'] = 'init, IsHardwareAccelerated=false' in logs
        result['gpu_frames'] = logs.count('GPU blit ')
        result['zero_copy_frames'] = logs.count('Zero-copy NV12 ')
        result['zero_copy_plane_imports'] = logs.count('zero-copy EGLImageTargetTexture2D succeeded')
        result['texture_copies'] = logs.count('successfully created texture via copy')
        result['max_outstanding_images'] = max(map(int, re.findall(r'outstanding (\d+)', logs)), default=0)
        slots = re.findall(r'Allocated RGBA pool slot (\d+)', logs)
        result['pool_slots'] = max(map(int, slots), default=-1) + 1
        result['pool_allocations'] = len(slots)
        result['codecs'] = sorted(set(re.findall(r'codec (\w+_mediacodec) :', logs)))
        result['copy_log_messages'] = logs.count('Failed to copy image data')
        if 'start' in result and 'end' in result:
            result['playback_cpu_seconds'] = result['end']['cpu_seconds'] - result['start']['cpu_seconds']
            result['playback_wall_seconds'] = result['end']['monotonic'] - result['start']['monotonic']
        result['passed'] = bool(result.get('completed') and not result.get('error')
            and result['mediacodec'] != args.software
            and (result['software_init'] if args.software else not result['software_init']))
        result['passed'] = bool(result['passed']
            and result['gpu_frames'] == 0
            and result['texture_copies'] == 0
            and (args.software or ((args.cpu_buffer or args.expect_cpu) and result['zero_copy_frames'] == 0)
                 or (not args.cpu_buffer and not args.expect_cpu
                     and result['zero_copy_frames'] >= expected_frames
                     and result['zero_copy_plane_imports'] > 0))
            and all(e['width'] == metadata['width'] and e['height'] == metadata['height'] and not e['error']
                    for e in result.get('events', []))
            and result.get('events', [{}])[0].get('decoded', 0) >= expected_frames
            and result.get('events', [{}])[-1].get('pixelRange', 0) > 100)
        (args.out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k != 'events'}))
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
