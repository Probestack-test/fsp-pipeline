"""Run selected build/test checks; bundle code stays in a constrained container."""
import hashlib, json, os, pathlib, re, shutil, subprocess, tempfile, urllib.request, zipfile
import xml.etree.ElementTree as ET
MAX_ZIP, MAX_EXPANDED, sequence, terminal_event_sent = 100*1024*1024, 500*1024*1024, 0, False

def event(stage, **details):
    global sequence, terminal_event_sent
    sequence += 1
    if stage in ('ERROR','COMPLETE'):terminal_event_sent=True
    body = dict(executionId=os.environ['EXECUTION_ID'], artifactSha256=os.environ['ARTIFACT_SHA256'], sequence=sequence, stage=stage, **details)
    endpoint = os.environ['SCAN_API_URL'].rstrip('/')+'/v2/scans/'+os.environ['SCAN_ID']+'/execution-events'
    if not endpoint.startswith('https://'): raise ValueError('SCAN_API_URL must use HTTPS')
    request = urllib.request.Request(endpoint, json.dumps(body).encode(), {'Content-Type':'application/json','Authorization':'Bearer '+os.environ['SCAN_RUNNER_TOKEN']})
    with urllib.request.urlopen(request, timeout=30) as response: response.read()

def extract(archive, root):
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist()) > 20000 or sum(e.file_size for e in bundle.infolist()) > MAX_EXPANDED: raise ValueError('Archive extraction limit exceeded')
        for entry in bundle.infolist():
            target=(root/entry.filename.replace('\\','/')).resolve()
            if not target.is_relative_to(root.resolve()) or (entry.external_attr>>16)&0o170000 == 0o120000: raise ValueError('Unsafe archive entry')
            if entry.is_dir(): target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with bundle.open(entry) as source,target.open('xb') as output: shutil.copyfileobj(source,output)

def coverage(reports):
    for report in sorted(reports.rglob('*')):
        if report.is_symlink() or not report.is_file() or report.stat().st_size>20*1024*1024: continue
        if report.name=='jacoco.xml':
            for counter in ET.parse(report).getroot().findall('counter'):
                if counter.get('type')=='LINE':
                    covered,missed=int(counter.get('covered')),int(counter.get('missed'))
                    if covered+missed:return 100*covered/(covered+missed)
        if report.name=='coverage.xml':
            rate=ET.parse(report).getroot().get('line-rate')
            if rate is not None:return 100*float(rate)
        if report.name=='lcov.info':
            text=report.read_text(); found=sum(map(int,re.findall(r'^LF:(\d+)$',text,re.M))); hit=sum(map(int,re.findall(r'^LH:(\d+)$',text,re.M)))
            if found:return 100*hit/found
    return None

def test_counts(reports):
    counts,found=dict(total=0,passed=0,failed=0,errors=0,skipped=0),False
    for report in reports.rglob('*.xml'):
        if report.name!='test-results.xml' and not report.name.startswith('TEST-'):continue
        for case in ET.parse(report).getroot().iter('testcase'):
            found=True; status='failed' if case.find('failure') is not None else 'errors' if case.find('error') is not None else 'skipped' if case.find('skipped') is not None else 'passed'
            counts[status]+=1;counts['total']+=1
    return counts if found else None

def make_workspace_writable(project):
    """Allow a rootful or rootless Docker daemon to write to the disposable bind mount."""
    paths=[project,*project.rglob('*')]
    for path in paths:
        if path.is_symlink():continue
        mode=path.stat().st_mode
        path.chmod(mode | 0o222 | (0o111 if path.is_dir() else 0))

def collect_reports(project, output):
    for report in project.rglob('*'):
        if report.name in ('jacoco.xml','coverage.xml','lcov.info','test-results.xml') or report.name.startswith('TEST-') and report.suffix=='.xml':
            if report.is_symlink() or not report.resolve().is_relative_to(project) or not report.is_file() or report.stat().st_size>20*1024*1024:continue
            destination=output/report.relative_to(project);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(report,destination)

def major_version(value, supported, label, default):
    if not value:return default
    match=re.search(r'(?<!\d)(\d+)(?:\.\d+)*',str(value))
    if not match:raise ValueError('Cannot determine '+label+' version from '+str(value))
    version=int(match.group(1));version=8 if version==1 and re.search(r'1\.8',str(value)) else version
    if version not in supported:raise ValueError('Unsupported '+label+' version '+str(version)+'; supported: '+','.join(map(str,supported)))
    return version

def maven_java_version(project):
    path=project/'.java-version'
    if path.exists():return major_version(path.read_text().strip(),(8,11,17,21,25),'Java',17)
    sdkman=project/'.sdkmanrc'
    if sdkman.exists():
        match=re.search(r'^java\s*=\s*([^\s]+)',sdkman.read_text(errors='replace'),re.M)
        if match:return major_version(match.group(1),(8,11,17,21,25),'Java',17)
    pom=(project/'pom.xml').read_text(errors='replace')
    values={name:re.search(r'<'+re.escape(name)+r'>\s*([^<]+)\s*</'+re.escape(name)+r'>',pom) for name in ('maven.compiler.release','java.version','maven.compiler.target','maven.compiler.source')}
    for name in values:
        match=values[name]
        if match:
            value=match.group(1).strip()
            property_ref=re.fullmatch(r'\$\{([^}]+)\}',value)
            if property_ref:
                resolved=re.search(r'<'+re.escape(property_ref.group(1))+r'>\s*([^<]+)\s*</'+re.escape(property_ref.group(1))+r'>',pom)
                value=resolved.group(1).strip() if resolved else value
            return major_version(value,(8,11,17,21,25),'Java',17)
    return 17

def node_version(project):
    for version_file in ('.nvmrc','.node-version'):
        path=project/version_file
        if path.exists():return major_version(path.read_text().strip(),(18,20,22,24),'Node',22)
    package=json.loads((project/'package.json').read_text())
    return major_version(package.get('engines',{}).get('node'),(18,20,22,24),'Node',22)

def python_version(project):
    value=None
    pyproject=project/'pyproject.toml'
    if pyproject.exists():
        match=re.search(r'requires-python\s*=\s*["\']([^"\']+)',pyproject.read_text(errors='replace'))
        if match:value=match.group(1)
    version=major_version(value,(3,),'Python',3)
    minor_match=re.search(r'3\.(\d+)',value or '')
    minor=int(minor_match.group(1)) if minor_match else 12
    if minor not in (9,10,11,12,13):raise ValueError('Unsupported Python version 3.'+str(minor))
    return str(version)+'.'+str(minor)

def container_identity_args():
    uid=getattr(os,'getuid',lambda:1000)();gid=getattr(os,'getgid',lambda:1000)()
    return ['--user',str(uid)+':'+str(gid),'--env','HOME=/tmp','--env','MAVEN_CONFIG=/tmp/.m2']

def plan(project,checks):
    if (project/'pom.xml').exists():
        commands=[]
        if 'COMPILE' in checks:commands.append(('BUILD','mvn -B clean -DskipTests compile'))
        if 'TEST' in checks:
            if 'COVERAGE' not in checks: command='mvn -B clean test'
            else:
                pom=(project/'pom.xml').read_text(errors='replace')
                # Generated projects may already configure JaCoCo. Attaching a second agent can
                # corrupt the argLine, so use the project's plugin when present.
                command=('mvn -B clean test org.jacoco:jacoco-maven-plugin:report'
                         if 'jacoco-maven-plugin' in pom else
                         'mvn -B clean org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent test org.jacoco:jacoco-maven-plugin:0.8.12:report')
            commands.append(('TEST',command))
        return 'maven:3.9.9-eclipse-temurin-'+str(maven_java_version(project)),commands
    if (project/'package.json').exists():
        scripts=json.loads((project/'package.json').read_text()).get('scripts',{});commands=[];install='npm ci --ignore-scripts'
        if 'COMPILE' in checks:
            build='npm run build' if scripts.get('build') else 'npm run typecheck' if scripts.get('typecheck') else None
            if build is None:raise ValueError('No existing Node build or typecheck script')
            commands.append(('BUILD',install+' && '+build));install='true'
        if 'TEST' in checks:
            if not scripts.get('test'):raise ValueError('No existing Node test script')
            commands.append(('TEST',install+' && '+('npm test -- --coverage' if 'COVERAGE' in checks else 'npm test')))
        return 'node:'+str(node_version(project))+'-bookworm-slim',commands
    install='if [ -f requirements.txt ]; then python -m pip install -r requirements.txt; fi; if [ -f pyproject.toml ]; then python -m pip install .; fi';commands=[]
    if 'COMPILE' in checks:commands.append(('BUILD',install+'; python -m compileall -q .'));install='true'
    if 'TEST' in checks:
        if not list(project.rglob('test_*.py')) and not list(project.rglob('*_test.py')):raise ValueError('No existing Python tests')
        packages='python -m pip install pytest pytest-cov' if 'COVERAGE' in checks else 'python -m pip install pytest';report=' --cov=. --cov-report=xml:coverage.xml' if 'COVERAGE' in checks else ''
        commands.append(('TEST',install+'; '+packages+'; python -m pytest --junitxml=test-results.xml'+report))
    return 'python:'+python_version(project)+'-slim',commands

def main():
    output=pathlib.Path('scan-output').resolve();output.mkdir(exist_ok=True)
    for key in ('SCAN_ID','EXECUTION_ID'):
        if not re.fullmatch(r'[a-zA-Z0-9-]+',os.environ[key]):raise ValueError('Invalid run identifier')
    checks=os.environ.get('EXECUTION_CHECKS','').split(',')
    if not checks or any(c not in ('COMPILE','TEST','COVERAGE') for c in checks) or len(checks)!=len(set(checks)):raise ValueError('Invalid execution checks')
    if 'COVERAGE' in checks and 'TEST' not in checks:raise ValueError('COVERAGE requires TEST')
    with tempfile.TemporaryDirectory(prefix='scan-exec-') as temporary:
        workspace=pathlib.Path(temporary);archive=workspace/'bundle.zip'
        with urllib.request.urlopen(os.environ['BUNDLE_URL'],timeout=120) as response,archive.open('wb') as target:
            total=0
            while chunk:=response.read(65536):
                total+=len(chunk)
                if total>MAX_ZIP:raise ValueError('Bundle exceeds size limit')
                target.write(chunk)
        with archive.open('rb') as source:
            if hashlib.file_digest(source,'sha256').hexdigest()!=os.environ['ARTIFACT_SHA256']:raise ValueError('Bundle checksum mismatch')
        root=workspace/'source';root.mkdir();extract(archive,root)
        candidates=sorted(set(p.parent for p in root.rglob('*') if p.name in ('pom.xml','package.json','pyproject.toml','requirements.txt') and not any(x in p.parts for x in ('node_modules','.venv','target'))),key=lambda p:len(p.parts))
        if not candidates:raise ValueError('No supported Maven, Node or Python project manifest found')
        project=candidates[0]
        if any(not p.is_relative_to(project) for p in candidates):raise ValueError('Multiple independent project roots require an explicit execution plan')
        for stale in project.rglob('*'):
            if stale.is_file() and (stale.name in ('jacoco.xml','coverage.xml','lcov.info','test-results.xml') or stale.name.startswith('TEST-') and stale.suffix=='.xml'):stale.unlink()
        image,commands=plan(project,checks)
        # Include the TemporaryDirectory parent as Docker must traverse every host path
        # component before it can enter the mounted project directory.
        make_workspace_writable(workspace)
        for index,(stage,command) in enumerate(commands):
            event(stage,runner=image,reason='Executing selected '+stage.lower()+' check');name='scan-'+os.environ['EXECUTION_ID']+'-'+str(index)
            args=['docker','run','--name',name,*container_identity_args(),'--cpus=2','--memory=2g','--pids-limit=256','--cap-drop=ALL','--security-opt=no-new-privileges','--mount',f'type=bind,src={project},dst=/workspace','--workdir=/workspace',image,'sh','-ec',command]
            try:
                with (output/(stage.lower()+'.log')).open('wb') as log:result=subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,timeout=1200)
            finally:subprocess.run(['docker','rm','-f',name],capture_output=True)
            collect_reports(project,output)
            if result.returncode!=0:
                log_path=output/(stage.lower()+'.log')
                tail=log_path.read_text(errors='replace')[-4000:] if log_path.exists() else ''
                measured=coverage(output) if 'COVERAGE' in checks else None
                event('ERROR',failedStage=stage,exitCode=result.returncode,
                      reason=stage+' execution failed',logTail=tail,
                      tests=test_counts(output),coveragePercent=measured)
                # Preserve the callback, but also make the GitHub job truthfully fail. Artifact
                # upload still runs because the workflow step uses `if: always()`.
                raise SystemExit(result.returncode)
        collect_reports(project,output)
        percent=None
        if 'COVERAGE' in checks:event('COVERAGE',reason='Parsing actual test-runner coverage reports');percent=coverage(output)
        event('COMPLETE',coveragePercent=percent,tests=test_counts(output),executedChecks=checks,reason='No coverage report produced' if 'COVERAGE' in checks and percent is None else 'Selected execution checks completed',workflowRunUrl=os.environ.get('GITHUB_SERVER_URL','')+'/'+os.environ.get('GITHUB_REPOSITORY','')+'/actions/runs/'+os.environ.get('GITHUB_RUN_ID',''))

if __name__=='__main__':
    try:main()
    except Exception as error:
        if not terminal_event_sent:event('ERROR',reason=type(error).__name__+': execution did not complete')
        raise
