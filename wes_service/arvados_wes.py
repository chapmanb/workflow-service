import arvados
import arvados.util
import arvados.collection
import arvados.errors
import os
import connexion
import json
import subprocess
import tempfile
import functools
import threading
import logging

from wes_service.util import visit, WESBackend


def get_api():
    return arvados.api_from_config(version="v1", apiconfig={
        "ARVADOS_API_HOST": os.environ["ARVADOS_API_HOST"],
        "ARVADOS_API_TOKEN": connexion.request.headers['Authorization'],
        "ARVADOS_API_HOST_INSECURE": os.environ.get("ARVADOS_API_HOST_INSECURE", "false"),  # NOQA
    })


statemap = {
    "Queued": "QUEUED",
    "Locked": "INITIALIZING",
    "Running": "RUNNING",
    "Complete": "COMPLETE",
    "Cancelled": "CANCELED"
}


def catch_exceptions(orig_func):
    """Catch uncaught exceptions and turn them into http errors"""

    @functools.wraps(orig_func)
    def catch_exceptions_wrapper(self, *args, **kwargs):
        try:
            return orig_func(self, *args, **kwargs)
        except arvados.errors.ApiError as e:
            logging.exception("Failure")
            return {"msg": e._get_reason(), "status_code": e.resp.status}, int(e.resp.status)
        except subprocess.CalledProcessError as e:
            return {"msg": str(e), "status_code": 500}, 500

    return catch_exceptions_wrapper

class ArvadosBackend(WESBackend):
    def GetServiceInfo(self):
        return {
            "workflow_type_versions": {
                "CWL": {"workflow_type_version": ["v1.0"]}
            },
            "supported_wes_versions": "0.2.1",
            "supported_filesystem_protocols": ["file", "http", "https", "keep"],
            "engine_versions": "cwl-runner",
            "system_state_counts": {},
            "key_values": {}
        }

    @catch_exceptions
    def ListWorkflows(self):
        api = get_api()

        requests = arvados.util.list_all(api.container_requests().list,
                                         filters=[["requesting_container_uuid", "=", None],
                                                  ["container_uuid", "!=", None]],
                                         select=["uuid", "command", "container_uuid"])
        containers = arvados.util.list_all(api.containers().list,
                                           filters=[["uuid", "in", [w["container_uuid"] for w in requests]]],
                                           select=["uuid", "state"])

        uuidmap = {c["uuid"]: statemap[c["state"]] for c in containers}

        return {
            "workflows": [{"workflow_id": cr["uuid"],
                           "state": uuidmap.get(cr["container_uuid"])}
                          for cr in requests
                          if cr["command"] and cr["command"][0] == "arvados-cwl-runner"],
            "next_page_token": ""
        }

    def invoke_cwl_runner(self, cr_uuid, workflow_url, workflow_params, env):
        try:
            with tempfile.NamedTemporaryFile() as inputtemp:
                json.dump(workflow_params, inputtemp)
                inputtemp.flush()
                workflow_id = subprocess.check_output(["arvados-cwl-runner", "--submit-request-uuid="+cr_uuid, # NOQA
                                                       "--submit", "--no-wait", "--api=containers",     # NOQA
                                                       workflow_url, inputtemp.name], env=env).strip()  # NOQA
        except subprocess.CalledProcessError as e:
            api = arvados.api_from_config(version="v1", apiconfig={
                "ARVADOS_API_HOST": env["ARVADOS_API_HOST"],
                "ARVADOS_API_TOKEN": env['ARVADOS_API_TOKEN'],
                "ARVADOS_API_HOST_INSECURE": env["ARVADOS_API_HOST_INSECURE"]  # NOQA
            })
            request = api.container_requests().update(uuid=cr_uuid, body={"priority": 0}).execute()  # NOQA

    @catch_exceptions
    def RunWorkflow(self, body):
        if body["workflow_type"] != "CWL" or body["workflow_type_version"] != "v1.0":  # NOQA
            return

        env = {
            "PATH": os.environ["PATH"],
            "ARVADOS_API_HOST": os.environ["ARVADOS_API_HOST"],
            "ARVADOS_API_TOKEN": connexion.request.headers['Authorization'],
            "ARVADOS_API_HOST_INSECURE": os.environ.get("ARVADOS_API_HOST_INSECURE", "false")  # NOQA
        }

        api = get_api()

        cr = api.container_requests().create(body={"container_request":
                                                   {"command": [""],
                                                    "container_image": "n/a",
                                                    "state": "Uncommitted",
                                                    "output_path": "n/a",
                                                    "priority": 500}}).execute()

        threading.Thread(target=self.invoke_cwl_runner, args=(cr["uuid"], body.get("workflow_url"), body["workflow_params"], env)).start()

        return {"workflow_id": cr["uuid"]}

    @catch_exceptions
    def GetWorkflowLog(self, workflow_id):
        api = get_api()

        request = api.container_requests().get(uuid=workflow_id).execute()
        if request["container_uuid"]:
            container = api.containers().get(uuid=request["container_uuid"]).execute()  # NOQA
        else:
            container = {"state": "Queued", "exit_code": None}

        outputobj = {}
        if request["output_uuid"]:
            c = arvados.collection.CollectionReader(request["output_uuid"], api_client=api)
            with c.open("cwl.output.json") as f:
                outputobj = json.load(f)

                def keepref(d):
                    if isinstance(d, dict) and "location" in d:
                        d["location"] = "%sc=%s/_/%s" % (api._resourceDesc["keepWebServiceUrl"], c.portable_data_hash(), d["location"])  # NOQA

                visit(outputobj, keepref)

        stderr = ""
        if request["log_uuid"]:
            c = arvados.collection.CollectionReader(request["log_uuid"], api_client=api)
            if "stderr.txt" in c:
                with c.open("stderr.txt") as f:
                    stderr = f.read()

        r = {
            "workflow_id": request["uuid"],
            "request": {},
            "state": statemap[container["state"]],
            "workflow_log": {
                "cmd": [""],
                "startTime": "",
                "endTime": "",
                "stdout": "",
                "stderr": stderr
            },
            "task_logs": [],
            "outputs": outputobj
        }
        if container["exit_code"] is not None:
            r["workflow_log"]["exit_code"] = container["exit_code"]
        return r

    @catch_exceptions
    def CancelJob(self, workflow_id):  # NOQA
        api = get_api()
        request = api.container_requests().update(uuid=workflow_id, body={"priority": 0}).execute()  # NOQA
        return {"workflow_id": request["uuid"]}

    @catch_exceptions
    def GetWorkflowStatus(self, workflow_id):
        api = get_api()
        request = api.container_requests().get(uuid=workflow_id).execute()
        if request["container_uuid"]:
            container = api.containers().get(uuid=request["container_uuid"]).execute()  # NOQA
        elif request["priority"] == 0:
            container = {"state": "Cancelled"}
        else:
            container = {"state": "Queued"}
        return {"workflow_id": request["uuid"],
                "state": statemap[container["state"]]}


def create_backend(opts):
    return ArvadosBackend(opts)
