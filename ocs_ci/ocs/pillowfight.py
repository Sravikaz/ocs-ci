"""
Pillowfight Class to run various workloads and scale tests
"""
import logging
import tempfile
import re
from os import listdir
from os.path import join
from shutil import rmtree
from ocs_ci.utility.spreadsheet.spreadsheet_api import GoogleSpreadSheetAPI

from ocs_ci.ocs.ocp import OCP
from ocs_ci.ocs.resources.ocs import OCS
from ocs_ci.ocs import constants
from ocs_ci.ocs.utils import get_pod_name_by_pattern
from ocs_ci.utility.utils import TimeoutSampler
from ocs_ci.utility import utils, templating

log = logging.getLogger(__name__)


class PillowFight(object):
    """
    Workload operation using PillowFight
    This class was modelled after the RipSaw class in this directory.
    """

    WAIT_FOR_TIME = 1800
    MIN_ACCEPTABLE_OPS_PER_SEC = 2000
    MAX_ACCEPTABLE_RESPONSE_TIME = 2000

    def __init__(self, **kwargs):
        """
        Initializer function

        Args:
            kwargs (dict):
                Following kwargs are valid
                repo: PillowFight repo to used - a github link
                branch: branch to use from the repo
                namespace: namespace for the operator

        Example Usage:
            r1 = PillowFight()
            r1.run_pillowfights()
            # To run a private yaml
            my_custom_bench = my_custom_bench.yaml
            run_cmd('oc apply -f my_custom_bench')
            # To get pillowfight data from log file
            data = r1.extract_data(log_file)
            # To do basic sanity checking of data
            r1.sanity_check(data)

        """
        self.args = kwargs
        self.namespace = self.args.get("namespace", "couchbase-operator-namespace")
        self.ocp = OCP()
        self.up_check = OCP(namespace=constants.COUCHBASE_OPERATOR)
        self.logs = tempfile.mkdtemp(prefix="pf_logs_")

    def run_pillowfights(self, replicas=1, num_items=None, num_threads=None):
        """
        loop through all the yaml files extracted from the pillowfight repo
        and run them.  Run oc logs on the results and save the logs in self.logs
        directory

        Args:
            replicas (int): Number of pod replicas
            num_items (int): Number of items to be loaded to the cluster
            num_threads (int): Number of threads

        """
        ocp_local = OCP(namespace=self.namespace)
        self.replicas = replicas
        for i in range(self.replicas):
            # for basic-fillowfight.yaml
            pfight = templating.load_yaml(constants.COUCHBASE_NEW_PILLOWFIGHT)
            pfight["metadata"]["name"] = "pillowfight-rbd-simple" + f"{i}"
            # change the name
            pfight["spec"]["template"]["spec"]["containers"][0]["command"][2] = (
                f"couchbase://cb-example-000{i}.cb-example."
                f"couchbase-operator-namespace.svc:8091/default?select_bucket=true"
            )
            # num of items
            pfight["spec"]["template"]["spec"]["containers"][0]["command"][4] = (
                str(num_items) if num_items else "20000"
            )
            # num of threads
            pfight["spec"]["template"]["spec"]["containers"][0]["command"][13] = (
                str(num_threads) if num_threads else "20"
            )
            lpillowfight = OCS(**pfight)
            lpillowfight.create()
        self.pods_info = {}

        for pillowfight_pods in TimeoutSampler(
            self.WAIT_FOR_TIME,
            9,
            get_pod_name_by_pattern,
            "pillowfight",
            constants.COUCHBASE_OPERATOR,
        ):
            try:
                counter = 0
                for pf_pod in pillowfight_pods:
                    pod_info = self.up_check.exec_oc_cmd(f"get pods {pf_pod} -o json")
                    pf_status = pod_info["status"]["containerStatuses"][0]["state"]
                    if "terminated" in pf_status:
                        pf_completion_info = pf_status["terminated"]["reason"]
                        if pf_completion_info == constants.STATUS_COMPLETED:
                            counter += 1
                            self.pods_info.update({pf_pod: pf_completion_info})
                    elif "running" in pf_status:
                        pass
                if counter == self.replicas:
                    break
            except IndexError:
                log.info("Pillowfight not yet completed")

        log.info(self.pods_info)
        for pod, pf_completion_info in self.pods_info.items():
            if pf_completion_info == "Completed":
                pf_endlog = f"{pod}.log"
                pf_log = join(self.logs, pf_endlog)
                data_from_log = ocp_local.exec_oc_cmd(
                    f"logs -f {pod} --ignore-errors", out_yaml_format=False
                )
                data_from_log = data_from_log.replace("\x00", "")
                with open(pf_log, "w") as fd:
                    fd.write(data_from_log)

            elif pf_completion_info == "Error":
                raise Exception("Pillowfight failed to complete")

    def analyze_all(self):
        """
        Analyze the data extracted into self.logs files

        """
        for path in listdir(self.logs):
            full_path = join(self.logs, path)
            log.info(f"Analyzing {full_path}")
            with open(full_path, "r") as fdesc:
                data_from_log = fdesc.read()
            log_data = self.parse_pillowfight_log(data_from_log)
            self.sanity_check(log_data)

    def sanity_check(self, stats):
        """
        Make sure the worst cases for ops per second and response times are
        within an acceptable range.

        """
        stat1 = min(stats["opspersec"])
        if stat1 < self.MIN_ACCEPTABLE_OPS_PER_SEC:
            raise Exception(f"Worst OPS/SEC value reported is {stat1}")
        stat2 = max(stats["resptimes"].keys()) / 1000
        if stat2 > self.MAX_ACCEPTABLE_RESPONSE_TIME:
            raise Exception(f"Worst response time reported is {stat2} milliseconds")

    def parse_pillowfight_log(self, data_from_log):
        """
        Run oc logs on the pillowfight pod passed in.  Cleanup the output
        from oc logs to handle peculiarities in the couchbase log results,
        and generate a summary of the results.

        The dictionary returned has two values; 'opspersec' and 'resptimes'.
        opspersec is a list of ops per second numbers reported.'
        resptimes is a dictionary index by the max response time of a range.
        Each entry in resptimes contains a minimum response time for that range,
        and a count of how many messages fall within that range.

        Args:
            data_from_log (str): log data

        Returns:
            dict: ops per sec and response time information

        """
        # The data in the couchbase logs is kind of abnormal.
        # It contains histograms with invalid unicode charaters for yaml
        # output (which is why out_yaml_format=False is used).
        # It also seems to write a block of text inside another block at
        # an unpredictable location.  The value good_txt below is the output
        # of the log with that data removed..
        #
        # So what's left is a list of OPS/SEC values and a histogram of
        # response times.  This routine organizes that data.

        ops_per_sec = []
        resp_hist = {}
        log.info("*******Couchbase raw output log*********\n" f"{data_from_log}")
        lines = data_from_log.split("\n")
        for dline in lines:
            try:
                if dline.startswith("OPS/SEC"):
                    dfields = dline.split(" ")
                    dnumb = int(dfields[-1].strip())
                    ops_per_sec.append(dnumb)
                if re.match("^\\[\\d+ +- \\d+ *\\][um]s \\|#* - \\d+", dline):
                    for element in ["[", "]", "|", "-", "#"]:
                        dline = dline.replace(element, " ")
                    parts = dline.split()
                    i1 = int(parts[0])
                    i2 = int(parts[1])
                    if parts[2] == "ms":
                        i1 *= 1000
                        i2 *= 1000
                    resp_hist[i2] = {"minindx": i1, "number": int(parts[3])}
            except ValueError:
                log.info(f"{dline} -- contains invalid data")
        ret_data = {"opspersec": ops_per_sec, "resptimes": resp_hist}
        return ret_data

    def export_pfoutput_to_googlesheet(self, sheet_name, sheet_index):
        """
        Collect pillowfight output to google spreadsheet

        Args:
            sheet_name (str): Name of the sheet
            sheet_index (int): Index of sheet

        """
        # Collect data and export to Google doc spreadsheet
        g_sheet = GoogleSpreadSheetAPI(sheet_name=sheet_name, sheet_index=sheet_index)
        log.info("Exporting pf data to google spreadsheet")
        for path in listdir(self.logs):
            full_path = join(self.logs, path)
            with open(full_path, "r") as fdesc:
                data_from_log = fdesc.read()
            log_data = self.parse_pillowfight_log(data_from_log)

            g_sheet.insert_row(
                [
                    f"{path}",
                    min(log_data["opspersec"]),
                    max(log_data["resptimes"].keys()) / 1000,
                ],
                2,
            )
        g_sheet.insert_row(["", "opspersec", "resptimes"], 2)

        # Capturing versions(OCP, OCS and Ceph) and test run name
        g_sheet.insert_row(
            [
                f"ocp_version:{utils.get_cluster_version()}",
                f"ocs_build_number:{utils.get_ocs_build_number()}",
                f"ceph_version:{utils.get_ceph_version()}",
                f"test_run_name:{utils.get_testrun_name()}",
            ],
            2,
        )

    def cleanup(self):
        """
        Remove pillowfight pods and temp files

        """
        rmtree(self.logs)
