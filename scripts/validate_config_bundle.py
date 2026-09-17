"""Validate non-code gateway/MCP bundles and emit a real JUnit result."""
import json, pathlib, sys, xml.etree.ElementTree as ET

root=pathlib.Path(sys.argv[2]).resolve();kind=sys.argv[1].upper();errors=[];checked=0

def files(*patterns):
    found=[]
    for pattern in patterns:found.extend(root.rglob(pattern))
    return sorted({path for path in found if path.is_file() and not path.is_symlink()})

try:
    if kind=='KONG':
        import yaml
        configs=[]
        for path in files('*.yaml','*.yml','*.json'):
            try:
                value=json.loads(path.read_text()) if path.suffix=='.json' else yaml.safe_load(path.read_text())
                if isinstance(value,dict) and ('_format_version' in value or 'services' in value or 'upstreams' in value):configs.append((path,value))
            except Exception as error:
                if 'kong' in path.name.lower():errors.append(str(path.relative_to(root))+': '+str(error))
        if not configs:errors.append('No Kong declarative configuration found')
        for path,value in configs:
            checked+=1
            if '_format_version' not in value:errors.append(str(path.relative_to(root))+': _format_version is required')
            for key in ('services','routes','upstreams','plugins'):
                if key in value and not isinstance(value[key],list):errors.append(str(path.relative_to(root))+': '+key+' must be a list')
    elif kind in ('APIGEE','PROXY') and (list(root.rglob('apiproxy')) or list(root.rglob('sharedflowbundle'))):
        xml_files=files('*.xml')
        if not xml_files:errors.append('No Apigee XML configuration found')
        for path in xml_files:
            checked+=1
            try:ET.parse(path)
            except Exception as error:errors.append(str(path.relative_to(root))+': '+str(error))
    elif kind=='MCP':
        manifests=files('mcp.json')
        if not manifests:errors.append('No mcp.json manifest or runnable project found')
        for path in manifests:
            checked+=1
            try:
                value=json.loads(path.read_text())
                if not isinstance(value,dict):errors.append(str(path.relative_to(root))+': manifest must be a JSON object')
            except Exception as error:errors.append(str(path.relative_to(root))+': '+str(error))
    else:
        import yaml
        specs=[]
        for path in files('*.yaml','*.yml','*.json'):
            try:
                value=json.loads(path.read_text()) if path.suffix=='.json' else yaml.safe_load(path.read_text())
                if isinstance(value,dict) and ('openapi' in value or 'swagger' in value):specs.append((path,value))
            except Exception:pass
        if not specs:errors.append('No runnable project or supported API/gateway configuration found')
        for path,value in specs:
            checked+=1
            if not isinstance(value.get('paths'),dict):errors.append(str(path.relative_to(root))+': paths object is required')
except Exception as error:errors.append(type(error).__name__+': '+str(error))

tests=max(checked,1);failures=1 if errors else 0
message='; '.join(errors)
suite=ET.Element('testsuite',{'name':'bundle-validation','tests':str(tests),'failures':str(failures)})
case=ET.SubElement(suite,'testcase',{'name':kind+' validation'})
if errors:ET.SubElement(case,'failure',{'message':message}).text=message
ET.ElementTree(suite).write(root/'test-results.xml',encoding='utf-8',xml_declaration=True)
if errors:
    print(message,file=sys.stderr);raise SystemExit(1)
print(kind+' bundle validation passed ('+str(checked)+' artifacts checked)')
