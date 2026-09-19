"""Run selected build/test checks; bundle code stays in a constrained container."""
import hashlib, json, os, pathlib, re, shutil, subprocess, tempfile, urllib.error, urllib.request, zipfile
import xml.etree.ElementTree as ET
MAX_ZIP, MAX_EXPANDED, sequence, terminal_event_sent = 100*1024*1024, 500*1024*1024, 0, False

def event(stage, **details):
    global sequence, terminal_event_sent
    sequence += 1
    body = dict(executionId=os.environ['EXECUTION_ID'], artifactSha256=os.environ['ARTIFACT_SHA256'], sequence=sequence, stage=stage, **details)
    endpoint = os.environ['SCAN_API_URL'].rstrip('/')+'/v2/scans/'+os.environ['SCAN_ID']+'/execution-events'
    if not endpoint.startswith('https://'): raise ValueError('SCAN_API_URL must use HTTPS')
    request = urllib.request.Request(endpoint, json.dumps(body).encode(), {'Content-Type':'application/json','Authorization':'Bearer '+os.environ['SCAN_RUNNER_TOKEN']})
    try:
        with urllib.request.urlopen(request, timeout=30) as response: response.read()
    except urllib.error.HTTPError as error:
        # Say what the service answered, so a rejected event can be diagnosed from the run's log.
        print('::error::Execution event '+stage+' was rejected: HTTP '+str(error.code)+' '+error.read().decode(errors='replace')[:500],flush=True)
        raise
    if stage in ('ERROR','COMPLETE'):terminal_event_sent=True

def send_final(stage, details, slim):
    """Send the closing event with everything the run produced; if the service refuses it (too large, or a field it
    cannot store), close the run with the essentials so it still finishes truthfully instead of hanging."""
    try:event(stage,**details)
    except urllib.error.HTTPError:
        print('::warning::Retrying the '+stage+' event without the suite list and logs',flush=True)
        event(stage,**slim)

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

ANSI=re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
LOG_LIMIT=24000

def clean_log(text):
    return ANSI.sub('',text)

def suite_details(reports):
    """Per-suite results and the failed cases, from the runner's own JUnit reports. Empty when it wrote none."""
    suites,failures={},[]
    for report in sorted(reports.rglob('*.xml')):
        if report.name!='test-results.xml' and not report.name.startswith('TEST-'):continue
        for case in ET.parse(report).getroot().iter('testcase'):
            name=case.get('classname') or report.stem
            entry=suites.setdefault(name,dict(name=name,total=0,passed=0,failed=0,errors=0,skipped=0,seconds=0.0))
            failure,error=case.find('failure'),case.find('error')
            status='failed' if failure is not None else 'errors' if error is not None else 'skipped' if case.find('skipped') is not None else 'passed'
            entry[status]+=1;entry['total']+=1
            try:entry['seconds']+=float(case.get('time') or 0)
            except ValueError:pass
            if status in ('failed','errors') and len(failures)<30:
                node=failure if failure is not None else error
                failures.append(dict(suite=name,name=case.get('name') or '',message=(node.get('message') or node.text or '').strip()[:500]))
    for entry in suites.values():entry['seconds']=round(entry['seconds'],3)
    return list(suites.values())[:100],failures

def words_to_counts(fragment):
    """'1 failed, 5 passed, 2 skipped' -> counts; total is the sum unless it is given."""
    counts=dict(total=0,passed=0,failed=0,errors=0,skipped=0)
    for number,word in re.findall(r'(\d+)\s+(passed|failed|skipped|todo|errors?|total)',fragment):
        number=int(number)
        if word=='total':counts['total']=number
        elif word.startswith('error'):counts['errors']+=number
        elif word=='todo':counts['skipped']+=number
        else:counts[word]+=number
    counted=counts['passed']+counts['failed']+counts['errors']+counts['skipped']
    counts['total']=max(counts['total'],counted)
    return counts if counts['total'] else None

def log_counts(text):
    """The runner's own summary line, for a project whose test tool wrote no JUnit report (Maven, Vitest, Jest, pytest)."""
    text=clean_log(text)
    maven=re.findall(r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)\s*$',text,re.M)
    if maven:
        total,failed,errors,skipped=map(int,maven[-1])
        return dict(total=total,passed=max(total-failed-errors-skipped,0),failed=failed,errors=errors,skipped=skipped)
    for pattern in (r'^\s*Tests\s{2,}(.+?)\s*\(\d+\)\s*$',r'^\s*Tests:\s+(.+)$',r'^=+\s+(.+?)\s+in\s+[\d.]+s.*=+\s*$'):
        found=re.findall(pattern,text,re.M)
        if found:
            counts=words_to_counts(found[-1])
            if counts:return counts
    return None

def read_logs(output):
    logs=[]
    for stage in ('build','test'):
        path=output/(stage+'.log')
        if path.exists():logs.append(dict(stage=stage,text=clean_log(path.read_text(errors='replace'))[-LOG_LIMIT:]))
    return logs

def run_details(output):
    """Everything worth showing about the run itself: counts, suites, failed cases and the full logs."""
    suites,failures=suite_details(output)
    counts=test_counts(output)
    logs=read_logs(output)
    if counts is None:counts=log_counts('\n'.join(log['text'] for log in logs if log['stage']=='test'))
    return dict(tests=counts,suites=suites,failures=failures,logs=logs)

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

def jacoco_agent_configured(project):
    """True only when an active build plugin execution attaches JaCoCo to tests."""
    try:
        root=ET.parse(project/'pom.xml').getroot()
        namespace=root.tag.partition('}')[0]+'}' if root.tag.startswith('{') else ''
        plugins=root.find(namespace+'build')
        plugins=None if plugins is None else plugins.find(namespace+'plugins')
        if plugins is None:return False
        for plugin in plugins.findall(namespace+'plugin'):
            artifact=plugin.find(namespace+'artifactId')
            if artifact is None or (artifact.text or '').strip()!='jacoco-maven-plugin':continue
            return any((goal.text or '').strip() in ('prepare-agent','prepare-agent-integration')
                       for goal in plugin.findall('.//'+namespace+'goal'))
    except ET.ParseError:return False
    return False

def node_test_command(project,scripts,coverage):
    """npm test; for Vitest also asks for the JUnit report (test counts per suite) and the lcov report (coverage)."""
    package=json.loads((project/'package.json').read_text())
    dependencies={**package.get('dependencies',{}),**package.get('devDependencies',{})}
    vitest='vitest' in dependencies or 'vitest' in scripts.get('test','')
    args=[]
    if vitest:args+=['--reporter=default','--reporter=junit','--outputFile.junit=test-results.xml']
    if coverage:args+=['--coverage']+(['--coverage.reporter=lcov','--coverage.reporter=text-summary'] if vitest else [])
    return 'npm test'+(' -- '+' '.join(args) if args else '')

def plan(project,checks,target_type=''):
    if (project/'pom.xml').exists():
        commands=[]
        if 'COMPILE' in checks:commands.append(('BUILD','mvn -B clean -DskipTests compile'))
        if 'TEST' in checks:
            if 'COVERAGE' not in checks: command='mvn -B clean test'
            else:
                report='org.jacoco:jacoco-maven-plugin:0.8.12:report'
                command=('mvn -B clean test '+report if jacoco_agent_configured(project) else
                         'mvn -B clean org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent test '+report)
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
            commands.append(('TEST',install+' && '+node_test_command(project,scripts,'COVERAGE' in checks)))
        return 'node:'+str(node_version(project))+'-bookworm-slim',commands
    install='if [ -f requirements.txt ]; then python -m pip install -r requirements.txt; fi; if [ -f pyproject.toml ]; then python -m pip install .; fi';commands=[]
    if 'COMPILE' in checks:commands.append(('BUILD',install+'; python -m compileall -q .'));install='true'
    if 'TEST' in checks:
        if not list(project.rglob('test_*.py')) and not list(project.rglob('*_test.py')):raise ValueError('No existing Python tests')
        packages='python -m pip install pytest pytest-cov' if 'COVERAGE' in checks else 'python -m pip install pytest';report=' --cov=. --cov-report=xml:coverage.xml' if 'COVERAGE' in checks else ''
        commands.append(('TEST',install+'; '+packages+'; python -m pytest --junitxml=test-results.xml'+report))
    return 'python:'+python_version(project)+'-slim',commands

def config_plan(project,checks,target_type):
    kind=target_type.upper()
    aliases={'APIGEE_PROXY':'APIGEE','APIGEE_SHARED_FLOW':'APIGEE','API_PROXY':'APIGEE','SHARED_FLOW':'APIGEE','KONG_GATEWAY_SERVICE':'KONG','MCP_SERVER':'MCP'}
    kind=aliases.get(kind,kind)
    if kind not in ('KONG','APIGEE','PROXY','MCP','API','OTHER'):raise ValueError('No runnable project found for target type '+target_type)
    dependency='python -m pip install --quiet PyYAML==6.0.2 && ' if kind!='MCP' else ''
    stage='TEST' if 'TEST' in checks else 'BUILD'
    return 'python:3.12-slim',[(stage,dependency+'python /runner/validate_config_bundle.py '+kind+' /workspace')]

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
        project=candidates[0] if candidates else root
        if candidates and any(not p.is_relative_to(project) for p in candidates):raise ValueError('Multiple independent project roots require an explicit execution plan')
        for stale in project.rglob('*'):
            if stale.is_file() and (stale.name in ('jacoco.xml','coverage.xml','lcov.info','test-results.xml') or stale.name.startswith('TEST-') and stale.suffix=='.xml'):stale.unlink()
        target_type=os.environ.get('TARGET_TYPE','').strip()
        config_execution=not candidates
        image,commands=plan(project,checks,target_type) if candidates else config_plan(project,checks,target_type)
        # Include the TemporaryDirectory parent as Docker must traverse every host path
        # component before it can enter the mounted project directory.
        make_workspace_writable(workspace)
        for index,(stage,command) in enumerate(commands):
            event(stage,runner=image,reason='Executing selected '+stage.lower()+' check');name='scan-'+os.environ['EXECUTION_ID']+'-'+str(index)
            validator=pathlib.Path(__file__).with_name('validate_config_bundle.py').resolve()
            args=['docker','run','--name',name,*container_identity_args(),'--cpus=2','--memory=2g','--pids-limit=256','--cap-drop=ALL','--security-opt=no-new-privileges','--mount',f'type=bind,src={project},dst=/workspace','--mount',f'type=bind,src={validator},dst=/runner/validate_config_bundle.py,readonly','--workdir=/workspace',image,'sh','-ec',command]
            try:
                with (output/(stage.lower()+'.log')).open('wb') as log:result=subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,timeout=1200)
            finally:subprocess.run(['docker','rm','-f',name],capture_output=True)
            collect_reports(project,output)
            if result.returncode!=0:
                log_path=output/(stage.lower()+'.log')
                tail=log_path.read_text(errors='replace')[-4000:] if log_path.exists() else ''
                measured=coverage(output) if 'COVERAGE' in checks else None
                details=run_details(output)
                send_final('ERROR',dict(failedStage=stage,exitCode=result.returncode,reason=stage+' execution failed',logTail=tail,
                                        coveragePercent=measured,**details),
                           dict(failedStage=stage,exitCode=result.returncode,reason=stage+' execution failed',logTail=tail,
                                coveragePercent=measured,tests=details['tests']))
                # Preserve the callback, but also make the GitHub job truthfully fail. Artifact
                # upload still runs because the workflow step uses `if: always()`.
                raise SystemExit(result.returncode)
        collect_reports(project,output)
        percent=None
        if 'COVERAGE' in checks:
            event('COVERAGE',reason='Configuration validation completed; line coverage is not applicable' if config_execution else 'Parsing actual test-runner coverage reports')
            if not config_execution:percent=coverage(output)
        reason=('Configuration validation completed; line coverage is not applicable' if config_execution and percent is None else
                'No coverage report produced' if 'COVERAGE' in checks and percent is None else 'Selected execution checks completed')
        details=run_details(output)
        send_final('COMPLETE',dict(coveragePercent=percent,executedChecks=checks,reason=reason,configValidation=config_execution,**details),
                   dict(coveragePercent=percent,executedChecks=checks,reason=reason,configValidation=config_execution,tests=details['tests']))

if __name__=='__main__':
    try:main()
    except Exception as error:
        if not terminal_event_sent:event('ERROR',reason=type(error).__name__+': execution did not complete')
        raise
