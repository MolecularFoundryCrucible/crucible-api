"""Serve a local browser probe for an automatically supplied signed Content-Length."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import time

from gcs_multipart_probe import BUCKET_PREFIX, Probe, ProbeFailure

PAGE = b'''<!doctype html><html><head><title>Multipart browser probe</title></head>
<body><pre id="status">Running</pre><script>
(async () => {
  let result;
  try {
    const capability = await (await fetch('/capability')).json();
    const response = await fetch(capability.url, {
      method: 'PUT', credentials: 'omit',
      headers: {'x-goog-hash': capability.checksum},
      body: new Uint8Array(capability.size).fill(98)
    });
    result = {status: response.status, etag_exposed: !!response.headers.get('etag'),
      checksum_exposed: !!response.headers.get('x-goog-hash'),
      user_agent: navigator.userAgent};
  } catch (error) {
    result = {error_type: error.name};
  }
  await fetch('/result', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(result)});
  document.getElementById('status').textContent = JSON.stringify(result);
})();
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--signer', required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(BUCKET_PREFIX + r'[a-z0-9-]+', args.bucket) or not re.fullmatch(r'mpu-stage1-[a-z0-9-]+@mf-crucible\.iam\.gserviceaccount\.com', args.signer):
        parser.error('Only disposable stage-1 resources are allowed')
    if not args.run:
        print('Plan only: run with --run and open the displayed localhost URL in a browser.')
        return 0
    args.project = 'mf-crucible'
    probe = Probe(args)
    browser_result = {}
    try:
        name = probe.prefix + 'browser'
        upload_id = probe.initiate(name)
        data = b'b' * 1024**2
        url, headers = probe.signed_part(name, upload_id, 1, data)
        capability = json.dumps({'url': url, 'checksum': headers['x-goog-hash'], 'size': len(data)}).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_body(self, body, content_type):
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == '/':
                    self.send_body(PAGE, 'text/html')
                elif self.path == '/capability':
                    self.send_body(capability, 'application/json')
                else:
                    self.send_error(404)

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                if self.path != '/result' or not 0 < length < 4096:
                    self.send_error(400)
                    return
                result = json.loads(self.rfile.read(length))
                for key in ('status', 'etag_exposed', 'checksum_exposed', 'user_agent', 'error_type'):
                    if key in result:
                        browser_result[key] = result[key]
                self.send_body(b'{}', 'application/json')

        with HTTPServer(('127.0.0.1', args.port), Handler) as server:
            server.timeout = 1
            print(f'Browser probe ready: http://127.0.0.1:{args.port}/', flush=True)
            deadline = time.monotonic() + 180
            while not browser_result and time.monotonic() < deadline:
                server.handle_request()
        probe.record('browser_signed_length_and_cors', browser_result.get('status') == 200 and browser_result.get('etag_exposed') and browser_result.get('checksum_exposed'), **browser_result)
    except Exception as exc:
        probe.record('browser_execution', False, error_type=type(exc).__name__, failed_check=str(exc) if isinstance(exc, ProbeFailure) else 'redacted')
    finally:
        probe.cleanup()
    report = {'recorded_at': datetime.now(timezone.utc).isoformat(), 'bucket': args.bucket, 'passed': all(r['passed'] for r in probe.results), 'checks': probe.results}
    payload = json.dumps(report, indent=2) + '\n'
    args.output.write_text(payload)
    print(payload)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
