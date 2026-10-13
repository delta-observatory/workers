"""Load the vendored contract schemas and check documents against them."""
from functools import cache
import json
from pathlib import Path

from jsonschema import Draft202012Validator

SUPPORTED_JOB_CONTRACTS = ('job/1',)
SUPPORTED_METHODS = {('tess-spoc-index', 1), ('tess-bls', 1)}


class ContractError(ValueError):
    pass


class Contracts:
    def __init__(self, root: Path):
        self.root = root

    @cache
    def schema(self, relative):
        return json.loads((self.root / relative).read_text())

    def check(self, relative, document):
        errors = sorted(Draft202012Validator(self.schema(relative)).iter_errors(document),
                        key=lambda e: list(e.absolute_path))
        if errors:
            e = errors[0]
            where = '/'.join(str(p) for p in e.absolute_path) or '(root)'
            raise ContractError(f'{relative}: {where}: {e.message}')

    def check_job(self, document):
        if document.get('contract') not in SUPPORTED_JOB_CONTRACTS:
            raise ContractError(f"unsupported job contract {document.get('contract')!r}")
        self.check('job/v1.schema.json', document)
        method = (document['method']['id'], document['method']['version'])
        if method not in SUPPORTED_METHODS:
            raise ContractError(f'unsupported method {method}')
        self.check(f'methods/{method[0]}/v{method[1]}.schema.json', document['parameters'])

    def check_result(self, document):
        self.check('result/v1.schema.json', document)

    def defaults(self, method_id, version):
        def build(schema):
            if schema.get('type') == 'object':
                return {k: build(v) for k, v in schema['properties'].items()}
            return schema['default']
        return build(self.schema(f'methods/{method_id}/v{version}.schema.json'))
