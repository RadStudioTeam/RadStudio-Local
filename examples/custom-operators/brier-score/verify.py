"""Offline lifecycle: package, once-mode execution, independent oracle, repeat/hash check."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import secrets
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import zipfile

import brier_operator  # noqa: F401
from radstudio.operator import BundleArtifactStore, invoke, OperatorRequest
from sklearn.metrics import brier_score_loss
from radstudio.operator_serve import OperatorServeHandler

HERE = Path(__file__).resolve().parent

def verify(destination):
    destination.mkdir(parents=True, exist_ok=True)
    rows = [{'imageId': f'i{i}', 'patientId': f'p{i}', 'label': y, 'probability': p}
            for i, (y, p) in enumerate([(0, .1), (1, .8), (0, .4), (1, .6)])]
    hashes = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = BundleArtifactStore(root / 'input')
        ref = store.write_json('predictions.json', {'records': rows, 'evaluation': 'out_of_fold'}, 'prediction_table')
        request = dict(requestId='research-brier', runId='synthetic', nodeId='audit',
                       operatorType='ResearchBrierScore', operatorVersion='1.0.0', parameters={},
                       inputs={'predictions': ref})
        payload = {'request': request, 'inputBundle': store.export_bundle()}
        source = root / 'request.json'
        source.write_text(json.dumps(payload), encoding='utf-8')
        for repeat in range(2):
            target = root / f'result-{repeat}.json'
            subprocess.run([sys.executable, str(HERE / 'serve.py'), '--once', '--in', str(source), '--out', str(target)], check=True)
            result = json.loads(target.read_text(encoding='utf-8'))['result']
            assert result['status'] == 'completed', result
            assert result['implementation']['resolvedVersion'] == '1.0.0'
            # Decode the transport bundle in a fresh workspace, just as a remote consumer does.
            output = BundleArtifactStore(root / f'output-{repeat}')
            output.import_bundle(result['outputBundle'])
            metrics = output.read_json(result['outputs']['metrics'])
            expected = brier_score_loss([r['label'] for r in rows], [r['probability'] for r in rows])
            assert abs(metrics['brier'] - expected) < 1e-14
            assert abs(metrics['brier'] - .0925) < 1e-14
            hashes.append(result['outputs']['metrics'])
        assert hashes[0] == hashes[1], 'Repeated output differs'
        token = secrets.token_hex(24)
        previous_token = os.environ.get('RADSTUDIO_OPERATOR_TOKEN')
        os.environ['RADSTUDIO_OPERATOR_TOKEN'] = token
        server = ThreadingHTTPServer(('127.0.0.1', 0), OperatorServeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f'http://127.0.0.1:{server.server_port}/invoke'
            body = json.dumps(payload).encode()
            try:
                urlopen(Request(endpoint, data=body), timeout=30)
                raise AssertionError('Unauthenticated invocation accepted')
            except HTTPError as error:
                assert error.code == 401
            with urlopen(Request(endpoint, data=body, headers={'Authorization': 'Bearer ' + token,
                         'Content-Type': 'application/json'}), timeout=30) as response:
                http_result = json.load(response)['result']
                assert http_result['status'] == 'completed'
                assert http_result['outputs']['metrics'] == hashes[0]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if previous_token is None:
                os.environ.pop('RADSTUDIO_OPERATOR_TOKEN', None)
            else:
                os.environ['RADSTUDIO_OPERATOR_TOKEN'] = previous_token
        for bad in ([], [rows[0], rows[0]], [dict(rows[0], probability=1.1)], [dict(rows[0], label=2)]):
            bad_ref = store.write_json('bad.json', {'records': bad}, 'prediction_table')
            bad_request = dict(request, inputs={'predictions': bad_ref})
            assert invoke(OperatorRequest.from_dict(bad_request), store).status == 'failed'
        wrong = dict(request, operatorVersion='9.9.9')
        assert invoke(wrong, store).status == 'failed'
    package = destination / 'research-brier-score-1.0.0.zip'
    names = ['brier_operator.py', 'serve.py', 'submission.json', 'Dockerfile', 'README.md', 'verify.py']
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, (HERE / name).read_bytes())
    evidence = dict(status='passed', scope='synthetic SDK once-mode and authenticated loopback HTTP; not cloud acceptance',
                    brier=metrics['brier'], outputReference=hashes[0], repeats=2,
                    negativeCases=6, python=sys.version, sklearn=importlib.metadata.version('scikit-learn'),
                    packageSha256=hashlib.sha256(package.read_bytes()).hexdigest(),
                    sources={name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in names},
                    pending=['authenticated platform submission/review/runtime binding', 'FC isolation and invocation', 'real cohort validation'])
    (destination / 'verification.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    verify(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / 'dist')
