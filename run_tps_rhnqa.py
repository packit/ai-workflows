#!/usr/bin/env python3

import os
import logging
from functools import cache
import requests
from requests_gssapi import HTTPSPNEGOAuth
import sys
from bkr.common.hub import HubProxy
from bkr.client import BeakerJob, BeakerRecipeSet, BeakerRecipe
from bkr.client.task_watcher import watch_tasks
import re
from collections import defaultdict
import xmlrpc.client
from lxml import etree

logger = logging.getLogger(__name__)


#configuration
ERRATA_URL = "https://errata.devel.redhat.com"
ERRATA_XMLRPC = "https://errata.devel.redhat.com/errata/errata_service"
ERRATA_TPS_XMLRPC = "https://errata.devel.redhat.com/tps/tps_service"
BEAKER_URL = "https://beaker.engineering.redhat.com"
CHECK_INSTALL_TASK = "/distribution/check-install"
SETUP_TASK = "/distribution/errata/setup"
TPS_TASK = "/distribution/errata/tps"
CLEANUP_TASK = "/distribution/errata/cleanup"



@cache
def ET_verify() -> bool | str:
    verify = os.getenv("REDHAT_IT_CA_BUNDLE")
    if verify:
        return verify
    else:
        return True


def ET_api_get(path: str, *, params: dict | None = None):
    url = f"{ERRATA_URL}/api/v1/{path}"
    response = requests.get(
        url,
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=ET_verify(),
        params=params,
    )
    response.raise_for_status()
    return response.json()


def get_errata_info(errata_id: int | str):
    server = xmlrpc.client.ServerProxy(ERRATA_XMLRPC)
    data = server.get_advisory_list({'id': str(errata_id)})

    if not data:
        raise ValueError(f"No errata found for '{errata_id}'")

    return data[0]


def get_tps_jobs(errata_id: int | str):
    url = f"{ERRATA_URL}/advisory/{errata_id}/tps_jobs.json"

    response = requests.get(
        url,
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=ET_verify(),
    )
    response.raise_for_status()
    data = response.json()

    #filter for RHNQA jobs only
    rhnqa_jobs = [job for job in data if job.get('rhnqa') is True]

    print(f"found {len(rhnqa_jobs)} RHNQA TPS jobs")
    return rhnqa_jobs


def get_beaker_hub():
    hub = HubProxy(
        conf={
            'HUB_URL': f'{BEAKER_URL}/RPC2',
            'AUTH_METHOD': 'krbv',
        },
        logger=logger,
    )
    return hub

#convert TPS version string to Beaker distro name
def clean_distro_name(version_str):
    clean = re.sub(r'^(RHEL-(?:Alt-)?\d+\.\d+)(?:\.\d+)?(?:\.(?:GA|MAIN|Z|EUS|AUS|TUS|E4S)).*$',
                   r'\1', version_str, flags=re.IGNORECASE)

    if "AppStream" in clean or "BaseOS" in clean:
        clean = re.sub(r'^(?:AppStream|BaseOS)-(\d+\.\d+).*', r'RHEL-\1', clean, flags=re.IGNORECASE)

    if re.match(r'^\d+\.\d+', clean):
        clean = f"RHEL-{clean}"

    #add .0 suffix if not present
    if re.match(r'^RHEL-\d+\.\d+$', clean):
        clean = clean + '.0'

    return clean

#determine RHEL variant from version string
def get_variant(version_str):
    v = version_str.lower()
    if "server" in v: return "Server"
    if "workstation" in v: return "Workstation"
    if "client" in v: return "Client"
    if "computenode" in v: return "ComputeNode"
    if "base" in v or "appstream" in v: return "BaseOS"
    return "Server"


def get_stable_profile(version_str, variant):
    #parse version string to extract major version
    match = re.search(r'(\d+)\.(\d+)', version_str)

    if not match:
        raise ValueError(f"could not parse RHEL version from '{version_str}'")

    major = match.group(1)

    #for TPS RHNQA tests use simple profile: stable-rhel-{major}-{variant}
    return f"stable-rhel-{major}-{variant.lower()}"


def generate_beaker_xml(errata_id, errata_uglyid, errata_synopsis, tps_jobs):
    #group TPS jobs by environment (distro, arch, variant)
    env_map = defaultdict(list)

    for job in tps_jobs:
        version_str = job['version']
        distro = clean_distro_name(version_str)
        arch = job['arch']
        variant = get_variant(version_str)

        #normalize arch
        if arch == "ppc":
            arch = "ppc64"

        key = (distro, arch, variant, version_str)  # Include version_str in key
        env_map[key].append(str(job['job_id']))

    if not env_map:
        raise ValueError("No RHNQA TPS jobs to generate XML for")

    #create BeakerJob with whiteboard and retention tag
    job = BeakerJob(
        whiteboard=f"[ER#{errata_id}](https://errata.devel.redhat.com/advisory/{errata_id}) - {errata_synopsis} - rhnqa",
        retention_tag="scratch"
    )

    #create a recipeSet for each environment
    for (distro, arch, variant, version_str), tps_ids in env_map.items():

        #create recipeSet
        recipe_set = BeakerRecipeSet(priority="Normal")

        #create recipe
        recipe = BeakerRecipe()
        recipe.set_whiteboard(f"{distro} {variant} {arch}")

        #add distro requirements
        recipe.addBaseRequires(
            distro=distro,
            variant=variant,
            role="None",
            ks_meta=""
        )

        #add arch requirement
        arch_node = recipe.doc.createElement('distro_arch')
        arch_node.setAttribute('op', '=')
        arch_node.setAttribute('value', arch)
        recipe.addDistroRequires(arch_node)

        #Task 1: /distribution/check-install
        recipe.addTask(
            task=CHECK_INSTALL_TASK,
            role="None",
            taskParams=[
                f"wow=run_tps_rhnqa.py {errata_id} --tps-rhnqa",
                "WOW_TASK_TYPE=setup"
            ]
        )

        #Task 2: /distribution/errata/setup
        #prepare the system for errata testing
        stable_profile = get_stable_profile(version_str, variant)
        recipe.addTask(
            task=SETUP_TASK,
            role="None",
            taskParams=[
                f"STABLE={stable_profile}",
                "UPDATE=false",
                "WOW_TASK_TYPE=setup"
            ]
        )

        #Task 3: /distribution/errata/tps
        #TPS RHNQA test
        recipe.addTask(
            task=TPS_TASK,
            role="None",
            taskParams=[
                "TPS_ID=None",
                f"DISTQA_ID={','.join(tps_ids)}",
                "ACTIONS=tps-rhnqa",
                f"ERRATA={errata_uglyid}"
            ]
        )

        # Task 4: /distribution/errata/cleanup
        recipe.addTask(
            task=CLEANUP_TASK,
            role="None",
            taskParams=[
                "WOW_TASK_TYPE=cleanup"
            ]
        )

        #add recipe to recipeSet
        recipe_set.addRecipe(recipe)

        #add recipeSet to job
        job.addRecipeSet(recipe_set)

    #convert to XML
    return job.toxml()


def submit_beaker_job(hub, beaker_xml, dry_run=False):
    if dry_run:
        print("[Dry Run] would submit this XML to Beaker")
        return None

    try:
        print("submitting job to Beaker")

        #submit the job
        job_id = hub.jobs.upload(beaker_xml)

        print(f"job submitted successfully, job ID: {job_id}")
        return job_id

    except Exception as e:
        print(f"job submission failed. Error: {e}")

        return None


def monitor_beaker_job(hub, job_id):

    taskspec = job_id

    print(f"watching job {taskspec}")
    print(f"view at: {BEAKER_URL}/jobs/{job_id.replace('J:', '')}")

    try:
        exit_code = watch_tasks(hub, [taskspec])
        return exit_code
    except KeyboardInterrupt:
        print(f"job is still running")
        print(f"check status at: {BEAKER_URL}/jobs/{job_id.replace('J:', '')}")
        return None
    except Exception as e:
        print(f"\nerror monitoring job: {e}")
        return None


def fetch_beaker_job_xml(hub, job_id):
    if not job_id.startswith("J:"):
        job_id = f"J:{job_id}"

    print(f"fetching job XML for {job_id}")

    #fetch XML from beaker
    xml_string = hub.taskactions.to_xml(
        job_id,
        False,  # flatten=False
        True,   # exclude_enclosing_tags=True
        True    # include_logs=True
    )

    # XML is bytes encoded as Unicode string encoding to binary
    xml_bytes = xml_string.encode('utf-8')

    #parse with lxml
    parser = etree.XMLParser(remove_blank_text=True)
    xml = etree.fromstring(xml_bytes, parser)

    return xml


class BeakerRecipeResult:

    def __init__(self, recipe_id):
        self.recipe_id = recipe_id

        # Recipe-level results
        self.total_recipe_result = None  # Pass/Fail/Warn
        self.total_recipe_status = None  # Completed/Running

        #task results
        self.install_result = None
        self.install_status = None
        self.errata_setup_result = None
        self.errata_setup_status = None
        self.errata_tps_result = None
        self.errata_tps_status = None
        self.errata_tps_task_id = None

        # TPS parameters
        self.tps_ids = []       # TPS_ID values
        self.distqa_ids = []    # DISTQA_ID values
        self.errata_id = None   # ERRATA parameter

        #additional info
        self.job_id = None
        self.distro = None
        self.arch = None
        self.variant = None

    def __repr__(self):
        return (f"BeakerRecipeResult(recipe_id={self.recipe_id}, "
                f"result={self.total_recipe_result}, "
                f"distqa_ids={self.distqa_ids})")

#extract task information from recipe XML element
def get_task_info(recipe_elem, task_type, item):
    task_name = f"/distribution/{task_type}"
    task = recipe_elem.find(f'.//*[@name="{task_name}"]')

    if task is not None:
        return task.get(item)
    return None

#extract TPS parameters (TPS_ID, DISTQA_ID, ERRATA) from /distribution/errata/tps task
def get_tps_params(recipe_elem):
    task_name = "/distribution/errata/tps"
    task = recipe_elem.find(f'.//*[@name="{task_name}"]')

    tps_ids = []
    distqa_ids = []
    errata_id = None

    if task is None:
        return (tps_ids, distqa_ids, errata_id)

    #extract parameters from <params><param> elements
    for param in task.findall('./params/param'):
        param_name = param.get('name')
        param_value = param.get('value', '')

        if param_name == 'TPS_ID' and param_value and param_value != 'None':
            #parse comma separated IDs
            tps_ids = [int(x.strip()) for x in param_value.split(',') if x.strip() and x.strip() != 'None']

        elif param_name == 'DISTQA_ID' and param_value and param_value != 'None':
            #parse comma separated IDs
            distqa_ids = [int(x.strip()) for x in param_value.split(',') if x.strip() and x.strip() != 'None']

        elif param_name == 'ERRATA' and param_value:
            errata_id = param_value

    return (tps_ids, distqa_ids, errata_id)

#load and parse data from Beaker job XML
def load_data_from_beaker_job(job_xml):
    recipe_list = []

    #get job level info
    job_id = job_xml.get('id')

    #iterate through all recipeSets and find recipe
    for recipeSet in job_xml.findall('./recipeSet'):
        recipe_elem = recipeSet.find('./recipe')

        if recipe_elem is None:
            continue

        #create BeakerRecipeResult obj
        recipe = BeakerRecipeResult(recipe_elem.get('id'))

        #fill in main information about recipe
        recipe.job_id = job_id
        recipe.total_recipe_result = recipe_elem.get('result')  # Pass/Fail/Warn
        recipe.total_recipe_status = recipe_elem.get('status')  # Completed/Running

        #get distro from recipe attributes (result XML) and distroRequires (submission XML)
        recipe.distro = recipe_elem.get('distro')
        if not recipe.distro:
            distro_elem = recipe_elem.find('./distroRequires/distro_name')
            if distro_elem is not None:
                recipe.distro = distro_elem.get('value')

        #get arch from recipe attributes (result XML) and distroRequires (submission XML)
        recipe.arch = recipe_elem.get('arch')
        if not recipe.arch:
            distro_arch = recipe_elem.find('./distroRequires/distro_arch')
            if distro_arch is not None:
                recipe.arch = distro_arch.get('value')

        #get variant from recipe attributes (result XML) and distroRequires (submission XML)
        recipe.variant = recipe_elem.get('variant')
        if not recipe.variant:
            distro_variant = recipe_elem.find('./distroRequires/distro_variant')
            if distro_variant is not None:
                recipe.variant = distro_variant.get('value')

        #get results and statuses of main tasks
        recipe.install_result = get_task_info(recipe_elem, 'check-install', 'result')
        recipe.install_status = get_task_info(recipe_elem, 'check-install', 'status')

        recipe.errata_setup_result = get_task_info(recipe_elem, 'errata/setup', 'result')
        recipe.errata_setup_status = get_task_info(recipe_elem, 'errata/setup', 'status')

        recipe.errata_tps_result = get_task_info(recipe_elem, 'errata/tps', 'result')
        recipe.errata_tps_status = get_task_info(recipe_elem, 'errata/tps', 'status')
        recipe.errata_tps_task_id = get_task_info(recipe_elem, 'errata/tps', 'id')

        #get TPS parameters from /distribution/errata/tps task
        tps_ids, distqa_ids, errata_id = get_tps_params(recipe_elem)
        recipe.tps_ids = tps_ids
        recipe.distqa_ids = distqa_ids
        recipe.errata_id = errata_id

        #validate that we have required parameters
        if not recipe.tps_ids and not recipe.distqa_ids:
            print(f"recipe {recipe.recipe_id} missing both TPS_ID and DISTQA_ID, skipping")
            continue

        #add to recipe list
        recipe_list.append(recipe)

    return recipe_list

#translate Beaker result to Errata Tool status.
def translate_beaker_result_to_errata_status(beaker_result):
    if beaker_result == "Pass":
        return "GOOD"
    elif beaker_result == "Fail":
        return "BAD"
    elif beaker_result in ["Warn", "Error", None]:
        return "ERROR"
    else:
        return "ERROR"


def upload_tps_results(tps_jobs, parsed_results, dry_run=False):
    #build mapping from job_id (DISTQA_ID) to run_id
    job_id_to_run_id = {}
    for job in tps_jobs:
        job_id_to_run_id[job['job_id']] = job['run_id']

    #connect to Errata Tool XML RPC service
    server = xmlrpc.client.ServerProxy(ERRATA_TPS_XMLRPC)

    uploaded_count = 0

    for result in parsed_results:
        #set DISTQA_IDs for this recipe
        distqa_ids = result.distqa_ids

        if not distqa_ids:
            print(f"recipe {result.recipe_id}: No DISTQA_ID found, skipping")
            continue

        #get the TPS result (use errata_tps_result, fallback to total_recipe_result)
        beaker_result = result.errata_tps_result
        if beaker_result is None or beaker_result == 'Unknown':
            beaker_result = result.total_recipe_result

        #translate to Errata Tool status
        errata_status = translate_beaker_result_to_errata_status(beaker_result)

        #workaround for nonexisting ET status Error
        if errata_status == 'ERROR':
            errata_status = 'BAD'

        #build recipe URL
        recipe_url = f"{BEAKER_URL}/recipes/{result.recipe_id}"

        #build message
        message = f"TPSinBeaker: {result.distro} {result.variant} {result.arch} - {beaker_result}"
        if len(message) > 255:
            message = message[:250] + "..."

        # Upload result for each DISTQA_ID
        for distqa_id in distqa_ids:
            run_id = job_id_to_run_id.get(distqa_id)

            if run_id is None:
                print(f"DISTQA_ID {distqa_id}: No matching run_id found, skipping")
                continue

            if dry_run:
                print(f"[DRY RUN] Would upload:")
                print(f"tps_id (DISTQA_ID): {distqa_id}")
                print(f"run_id: {run_id}")
                print(f"status: {errata_status}")
                print(f"url: {recipe_url}")
                print(f"message: {message}")
                print()
            else:
                try:
                    server.jobReport(distqa_id, run_id, errata_status, recipe_url, message)
                    print(f"DISTQA_ID {distqa_id}: {errata_status}")
                    uploaded_count += 1
                except Exception as e:
                    print(f"DISTQA_ID {distqa_id}: {e}")

    if not dry_run:
        print(f"\nall results were saved into Errata Tool ({uploaded_count} uploaded)")

    return uploaded_count


def main():

    if len(sys.argv) < 2:
        sys.exit(1)

    errata_id = sys.argv[1]

    print(f"Step 1: fetching errata info for {errata_id}")
    errata_info = get_errata_info(errata_id)

    #extract uglyid from advisory_name
    advisory_name = errata_info.get('advisory_name', '')
    errata_uglyid = advisory_name.split("-")[1] if advisory_name else str(errata_id)

    #errata synopsis for whiteboard
    errata_synopsis = errata_info.get('synopsis', 'errata update')


    #get TPS jobs from Errata Tool
    print(f"\nStep 2: fetching TPS jobs for erratum {errata_id}")
    tps_jobs = get_tps_jobs(errata_id)

    if not tps_jobs:
        print(f"no RHNQA TPS jobs found for erratum {errata_id}")
        sys.exit(0)

    print(f"RHNQA TPS jobs:")
    for job in tps_jobs:
        print(f"  - Job ID: {job['job_id']}, Version: {job['version']}, Arch: {job['arch']}")

    #authenticate with Beaker
    print(f"\nStep 3: authenticating with Beaker")
    hub = get_beaker_hub()

    #test authentication
    try:
        user = hub.auth.who_am_i()
        print(f"authenticated as: {user['username']}")
    except Exception as e:
        print(f"authentication failed: {e}")
        sys.exit(1)

    #generate Beaker job XML using client library
    print(f"\nStep 4: generating Beaker job XML")
    beaker_xml = generate_beaker_xml(errata_id, errata_uglyid, errata_synopsis, tps_jobs)
    print(f"generated XML for {len(tps_jobs)} TPS jobs")

    #submit job to Beaker
    print(f"\nStep 5: submitting job to Beaker")

    #set dry_run=False to actually submit
    DRY_RUN =False
    job_id = submit_beaker_job(hub, beaker_xml, dry_run=DRY_RUN)

    if DRY_RUN and not job_id:
        print(f"dry run complete.")
        sys.exit(0)

    if not job_id:
        print("failed to submit job")
        sys.exit(1)

    print(f"submitted Beaker job {job_id}")

    #monitor job status
    print(f"\nStep 6: monitoring job status")
    exit_code = monitor_beaker_job(hub, job_id)

    if exit_code is None:
        print(f"monitoring was interrupted job is still running at: {BEAKER_URL}/jobs/{job_id}")
        sys.exit(0)

    if exit_code != 0:
        print(f"job failed with exit code: {exit_code}")
        sys.exit(1)

    print(f"Beaker job completed successfully!")

    #parse Beaker results
    print(f"\nStep 7: parsing Beaker job results")
    job_xml = fetch_beaker_job_xml(hub, job_id)
    recipe_list = load_data_from_beaker_job(job_xml)

    print(f"parsed {len(recipe_list)} recipe(s):")
    for recipe in recipe_list:
        print(f"Recipe: {recipe}")

    #upload results to Errata Tool
    print(f"\nStep 8:uploading results to Errata Tool")

    #Set to True to test without actually uploading
    UPLOAD_DRY_RUN = False

    uploaded = upload_tps_results(tps_jobs, recipe_list, dry_run=UPLOAD_DRY_RUN)
    if UPLOAD_DRY_RUN:
        print(f"[Dry Run]would have uploaded {len(recipe_list)} results")
    else:
        print(f"uploaded {uploaded} results to Errata Tool!")

    print("\nTPS RHNQA workflow complete!")

if __name__ == "__main__":
    main()
