from dataclasses import dataclass, field
from typing import Any

import requests


import os


@dataclass
class StepResult:
    """Uniform result of any API call, regardless of which tool produced it."""

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    text: str = ""
    url: str = ""
    method: str = ""

    def json(self) -> Any:
        return self.body


class BaseClient:
    """Thin HTTP layer. Knows nothing about OpenAPI or flows -- just makes
    requests and injects auth headers it's handed."""

    def __init__(self, base_url: str, testing_api_key: str | None = None,
                 session: requests.Session | None = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.testing_api_key = testing_api_key
        self.session = session or requests.Session()
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | list | None = None,
        headers: dict | None = None,
    ) -> StepResult:
        url = f"{self.base_url}{path}"
        opened_files = []
        prepared_files = None
        if files:
            prepared_files = {}
            if isinstance(files, dict):
                for k, v in files.items():
                    if isinstance(v, str) and (v.startswith("@") or os.path.isfile(v)):
                        fpath = v[1:] if v.startswith("@") else v
                        if os.path.isfile(fpath):
                            f = open(fpath, "rb")
                            opened_files.append(f)
                            prepared_files[k] = (os.path.basename(fpath), f)
                        else:
                            prepared_files[k] = (os.path.basename(fpath), v)
                    elif isinstance(v, (bytes, bytearray)):
                        prepared_files[k] = (f"{k}.bin", v)
                    elif isinstance(v, tuple) and len(v) >= 2:
                        fname, fcontent = v[0], v[1]
                        extra = v[2:]
                        if isinstance(fcontent, str) and (fcontent.startswith("@") or os.path.isfile(fcontent)):
                            fpath = fcontent[1:] if fcontent.startswith("@") else fcontent
                            if os.path.isfile(fpath):
                                f = open(fpath, "rb")
                                opened_files.append(f)
                                prepared_files[k] = (fname, f, *extra)
                            else:
                                prepared_files[k] = v
                        else:
                            prepared_files[k] = v
                    else:
                        prepared_files[k] = v
            else:
                prepared_files = files

        req_headers = dict(headers) if headers else None
        if prepared_files is not None and req_headers:
            req_headers = {k: v for k, v in req_headers.items() if not (k.lower() == "content-type" and "multipart/form-data" in v.lower())}

        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json,
                data=data,
                files=prepared_files,
                headers=req_headers,
                timeout=self.timeout,
            )
        finally:
            for f in opened_files:
                try:
                    f.close()
                except Exception:
                    pass

        try:
            body = response.json()
        except ValueError:
            body = None
        return StepResult(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=body,
            text=response.text,
            url=url,
            method=method,
        )
