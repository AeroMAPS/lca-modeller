import json
import os
from importlib.resources import open_text
from abc import ABC, abstractmethod
import shutil

import bw2data
import bw2calc
import bw2io
from bw2io.importers import ExcelLCIAImporter, CSVLCIAImporter
from jsonschema import validate
import lca_algebraic as agb
from sympy import sympify
import logging
import os.path as pth
from ruamel.yaml import YAML
from dotenv import load_dotenv
import premise as pm

from lca_modeller.io import resources
from lca_modeller.interpolation import interpolate_activities
from lca_algebraic.params import (
    ParamDef, all_params
)
from lca_algebraic.activity import ActivityOrActivityAmount, newActivity, ActivityExtended
from typing import Dict, List, Union, Tuple
from functools import reduce
from collections import defaultdict
from lca_modeller.helpers import safe_delete_brightway_project, confirm_reset

BIOSPHERE3_DB_NAME = "biosphere3"
USER_BIOSPHERE_DB_NAME = "lca_modeller_user_flows"

_LOGGER = logging.getLogger(__name__)

JSON_SCHEMA_NAME = "configuration.json"
USER_DB = 'Foreground DB'
KEY_PROJECT = 'project'
KEY_ECOINVENT = 'ecoinvent'
KEY_VERSION = 'version'
KEY_MODEL = 'model'
KEY_NORMALIZED_MODEL = 'normalized model'
KEY_EXCHANGE = 'amount'
KEY_SWITCH = 'is_switch'
KEY_NAME = 'name'
KEY_PARAMETER = 'parameter'
KEY_LOCATION = 'loc'
KEY_CATEGORIES = 'categories'
KEY_UNIT = 'unit'
KEY_CUSTOM_ATTR = 'custom_attributes'
KEY_ATTR_NAME = 'attribute'
KEY_ATTR_VALUE = 'value'
KEY_TAGS = 'tags'
EXCHANGE_DEFAULT_VALUE = 1.0  # default value for the exchange between two activities
SWITCH_DEFAULT_VALUE = False  # default foreground activity type (True: it is a switch activity; False: it is a regular activity)
FLOAT_PARAM_DEFAULT_VALUE = 0.0  # default value for float parameters
KEY_METADATA = 'parameters_metadata'
KEY_METHODS = 'methods'
KEY_CUSTOM_METHODS = 'custom_methods'
KEY_PREMISE = 'premise'
KEY_SCENARIOS = 'scenarios'
KEY_YEAR = 'year'
KEY_PATHWAY = 'pathway'
KEY_UPDATE_PREMISE = 'update'
KEY_UPDATE_ACT = 'update'
KEY_INPUT_ACT = 'input_activity'
KEY_NEW_VAL = 'new_value'
KEY_DELETE = 'delete'
KEY_ADD = 'add'
KEY_FILEPATH = 'filepath'
KEY_SOURCE_METHOD = 'source_method'
DEFAULT_DESC_LCIA = 'custom LCIA method'
KEY_RESET = 'reset_project'


def _get_foreground_activity(name):
    """Return foreground activity with exact name, or None."""
    for act in bw2data.Database(USER_DB):
        if act.get("name") == name:
            return act
    return None


def _foreground_activity_exists(name):
    return _get_foreground_activity(name) is not None


def _get_unique_activity_name(key):
    i = 1
    while _foreground_activity_exists(f"{key}_{i}"):
        i += 1
    return f"{key}_{i}"


def _reset_db_and_proxy(db_name: str, foreground: bool = True):
    """
    Reset a database and remove any lca-algebraic proxy database
    derived from it.

    Proxy databases contain implementation-specific activities whose
    exchanges may become invalid when the source database is rebuilt.
    """
    proxy_db_name = f"{db_name}-proxy"

    if proxy_db_name in bw2data.databases:
        _LOGGER.debug(
            "Deleting stale proxy database '%s' before resetting '%s'.",
            proxy_db_name,
            db_name,
        )
        agb.deleteDb(proxy_db_name)

    _LOGGER.debug(
        "Resetting database '%s' (foreground=%s).",
        db_name,
        foreground,
    )

    agb.resetDb(
        db_name,
        foreground=foreground,
    )


def is_comma_separated(file_path):
    """
    Checks if the csv file is well formated, i.e. it contains commas and not semicolons.
    :param file_path:
    :return:
    """
    with open(file_path, 'r') as file:
        first_line = file.readline()
        return ',' in first_line


def _parse_exchange(table: dict, act: ActivityExtended = None, params_meta_dict: dict = None):
    """
    Gets the exchange expression from the table of options for the activity, then creates the input parameters and returns the symbolic expression of the exchange

    :param table: the table of options for the activity.
    :param act: the activity itself. Only used if special formula sum() is used in the exchange amount.
    :param params_meta_dict: metadata of the parameters, as defined in a specific section of the configuration file.
    :return expr: the symbolic expression.
    """
    params_meta_dict = params_meta_dict if params_meta_dict else {}

    # Get the 'amount' value of the exchange. If it doesn't exist then set the exchange quantity to 1
    exchange = table.get(KEY_EXCHANGE, EXCHANGE_DEFAULT_VALUE)

    # Special formula sum() to sum the amounts of all exchanges with a given name in the activity
    # E.g. 'sum(electricity*)' will sum the amounts of all exchanges with the name 'electricity' (whatever the location)
    if 'sum(' in str(exchange) and act:
        selected_exchanges = str(exchange).split('sum(')[1].split(')')[0]
        if selected_exchanges:
            sum_value = str(sum_amounts(act, selected_exchanges))
            exchange = str(exchange).replace(f'sum({selected_exchanges})', sum_value)

    # Parse the string expression
    expr = sympify(exchange)

    # Get the parameters involved in the expression
    parameters = expr.free_symbols

    # Create new parameters if necessary
    for param in parameters:
        if param == agb.old_amount:  # skip the special 'old_amount' parameter
            continue
        if param not in all_params():  # create the parameter if it doesn't exist
            # Get the metadata of the parameter if provided in specific section of the configuration file
            param_meta = params_meta_dict.get(param.name, {})
            default = param_meta.get('default', FLOAT_PARAM_DEFAULT_VALUE)
            min_val = param_meta.get('min', 0.0)
            max_val = param_meta.get('max', 1.0)
            unit = param_meta.get('unit', None)
            description = param_meta.get('description', None)
            distrib = param_meta.get('distrib', agb.DistributionType.LINEAR)
            formula = sympify(param_meta.get('formula', None))
            label = param_meta.get('label', None)
            group = param_meta.get('group', None)

            # Create new parameter
            agb.newFloatParam(
                param.name,  # name of the parameter
                default=default,  # default value
                min=min_val,  # for sensitivity analysis runs
                max=max_val,  # for sensitivity analysis runs
                unit=unit,
                description=description,
                distrib=distrib,  # distribution type for uncertainty analysis runs
                formula=formula,
                label=label,
                group=group,
                dbname=USER_DB  # we define the parameter in the foreground database
            )

    return expr


def _create_activity_wrapper(
    activity,
    name,
    custom_attributes=None,
    tags=None,
):
    """Create a lightweight foreground activity pointing 1:1
    to an existing activity.

    Used to attach metadata without copying the activity inventory.
    """
    custom_attributes = custom_attributes or []
    tags = tags or []

    wrapper = agb.newActivity(
        db_name=USER_DB,
        name=name,
        unit=activity["unit"],
        exchanges={activity: 1.0},
    )

    for attr in custom_attributes:
        wrapper.updateMeta(
            **{
                attr.get(KEY_ATTR_NAME):
                attr.get(KEY_ATTR_VALUE)
            }
        )

    for tag in tags:
        wrapper.updateMeta(**tag)

    return wrapper


def sum_amounts(act, exchange_name):
    """
    Sums the amounts of all exchanges with the same name in the activity.
    Useful when exchange_name contains wildcard `*` to refer to multiple exchanges.
    :param act: activity to sum the exchanges amounts
    :param exchange_name: name of the exchange to sum. Can be a wildcard `*` to refer to multiple exchanges.
    :return: the sum of all exchanges amounts
    """
    exchs = act.getExchange(exchange_name, single=False)
    total = sum(exch['amount'] for exch in exchs)
    return total


def newMultiSwitchAct(dbname, name, paramDefList: Union[List[ParamDef], ParamDef],
                      acts_dict: Union[
                          Dict[str, ActivityOrActivityAmount], Dict[Tuple[str], ActivityOrActivityAmount]]):
    """
    Creates a new switch activity with multiple switch parameters.
    A child activity is selected based on the combination of the switch parameters.
    This is a modification of newSwitchAct from lca_algebraic library.

    Parameters
    ----------
    dbname:
        name of the target DB
    name:
        Name of the new activity
    paramDefList :
        List of enum parameters
    acts_dict :
        dict of (("enum_1_value", ..., "enum_n_value") => activity or (activity, amount)

    Examples
    --------
    > newMultiSwitchAct(MYDB, "MultiSwitchAct", [switchParam1, switchParam2], {
    >    ("switchParam1_val1", switchParam2_val1) : act1  # Amount is 1
    >    ("switchParam1_val2", switchParam2_val2) : (act2, 0.4) # Different amount
    >    ("switchParam1_val1", switchParam2_val2) : (act3, b + 6) # Amount with formula
    > }
    """
    # Transform map of enum values to corresponding formulas <param_name>_<enum_value>
    exch = defaultdict(lambda: 0)

    # Forward last unit as unit of the switch
    unit = None
    for key, act in acts_dict.items():
        amount = 1
        if isinstance(act, (list, tuple)):
            act, amount = act
        if isinstance(paramDefList, list):
            exch[act] += amount * reduce(lambda x, y: x * y,
                                         [paramDef.symbol(key[i]) for i, paramDef in enumerate(paramDefList)])
        else:
            exch[act] += amount * paramDefList.symbol(key)  # Regular switch activity
        unit = act["unit"]

    res = newActivity(dbname, name, unit=unit, exchanges=exch)

    return res


def create_custom_lcia_method(name: str, filepath: str, unit: str = None, source_method: str = None):
    """
    Creates a new LCIA method from an Excel file.

    :param name: name of the new method
    :param filepath: path to the Excel file
    :param unit: unit of the method
    :param source_method: name of the source method to duplicate and modify from. If not provided, the method is created from scratch.
    """

    if name in bw2data.methods and bw2data.methods.get(name).get('description') != DEFAULT_DESC_LCIA:
        # TODO: find a better way to check if method is a default one
        _LOGGER.warning(f"Method {name} already exists as default LCIA method and is protected. "
             f"If you wish to duplicate and modify, use `{KEY_SOURCE_METHOD}` option.")
        return

    _LOGGER.info("Creating custom LCIA method: %s", name)

    file_name, file_extension = os.path.splitext(filepath)
    if file_extension not in ['.csv', '.xlsx']:
        raise ValueError("File extension not supported. Please provide a CSV or Excel file.")
    if file_extension == '.csv' and not is_comma_separated(filepath):
        raise ValueError("CSV file must be comma-separated. Please convert the file to a CSV with commas.")

    if source_method:
        # BW25 ecoinvent methods can be namespaced, e.g.
        # ('ecoinvent-3.10', ..., ..., ...) instead of the legacy (..., ..., ...) tuple
        source_method = resolve_method(source_method)

        source_method_unit = (
            bw2data.Method(source_method)
            .metadata
            .get("unit")
        )

        if unit != source_method_unit:
            _LOGGER.warning(
                f"Unit `{unit}` provided for method {name} "
                f"is different from source method. "
                f"Overriding with source method unit "
                f"`{source_method_unit}`."
            )

        unit = (
            source_method_unit
            if unit != source_method_unit or not unit
            else unit
        )

    Importer = CSVLCIAImporter if file_extension == '.csv' else ExcelLCIAImporter
    newLCIA = Importer(filepath, name, DEFAULT_DESC_LCIA, unit)

    # Link with existing biosphere flows
    newLCIA.apply_strategies(verbose=False)

    # If remaining flows: create biosphere database to define new flows
    if newLCIA.statistics(print_stats=False)[2] != 0:
        # User biosphere to store new flows (e.g. contrails)
        user_biosphere = bw2data.Database(USER_BIOSPHERE_DB_NAME)
        user_biosphere.write(dict())

        # Link with existing flows in user biosphere
        super(ExcelLCIAImporter, newLCIA).__init__(
            filepath,
            biosphere=USER_BIOSPHERE_DB_NAME
        )  # Waiting for bw2io fix on that (issue #249)
        newLCIA.apply_strategies(verbose=False)

        # Add missing flows and link
        super(ExcelLCIAImporter, newLCIA).__init__(
            filepath,
            biosphere=USER_BIOSPHERE_DB_NAME
        )  # for some reason instance has to be reset, probably to reinit strategy functions with latest data available.
        newLCIA.add_missing_cfs()  # add missing flows to dedicated biosphere db
        newLCIA.apply_strategies(verbose=False)

    # Add additional data from source method
    if source_method:
        source_cfs = bw2data.Method(source_method)

        for method in newLCIA.data:
            new_cfs_flows = set()

            for cf in method["exchanges"]:
                if "input" not in cf:
                    continue

                flow = cf["input"]

                if isinstance(flow, int):
                    flow = bw2data.get_node(id=flow).key
                elif isinstance(flow, list):
                    flow = tuple(flow)

                new_cfs_flows.add(flow)

            for source_cf in source_cfs:
                flow_data = source_cf[0]
                cf_value = source_cf[1]

                flow_key = flow_data.key

                if flow_key not in new_cfs_flows:
                    method["exchanges"].append(
                        {
                            "name": flow_data.get("name"),
                            "categories": flow_data.get("categories"),
                            "amount": cf_value,
                            "unit": flow_data.get("unit"),
                            "type": flow_data.get("type"),
                            "code": flow_data.get("code"),
                            "input": flow_key,
                        }
                    )

    # Write new LCIA method
    newLCIA.statistics()
    newLCIA.write_methods(overwrite=True)

    # Write excel file of new method updated with data from source method
    if source_method:
        fp_source = bw2io.export.excel.write_lcia_matching(newLCIA, file_name)
        fp_destination = os.path.abspath(file_name) + '_updated' + '.xlsx'
        shutil.move(fp_source, fp_destination)
        _LOGGER.info(
            "LCIA matching file written to: %s",
            fp_destination,
        )


def resolve_method(method):
    """Resolve a LCIA method against the current Brightway project.

    Supports both legacy unnamespaced methods and BW25/ecoinvent
    methods prefixed with an ecoinvent namespace.
    """
    method = tuple(method)

    # Exact match: legacy BW projects or unnamespaced BW25
    if method in bw2data.methods:
        return method

    # BW25 namespaced methods, e.g.
    # ('ecoinvent-3.9.1', ...) + legacy method tuple
    matches = [
        candidate
        for candidate in bw2data.methods
        if len(candidate) >= len(method)
        and tuple(candidate[-len(method):]) == method
    ]

    if len(matches) == 1:
        return matches[0]

    if not matches:
        raise ValueError(
            f"LCIA method {method!r} not found in Brightway project "
            f"{bw2data.projects.current!r}"
        )

    raise ValueError(
        f"LCIA method {method!r} is ambiguous. Matches: {matches}"
    )


class LCAProblemConfigurator:
    """
    class for configuring an LCA_algebraic problem from a configuration file

    See :ref:`description of configuration file <configuration-file>`.

    :param conf_file_path: if provided, configuration will be read directly from it
    """

    def __init__(self, conf_file_path=None):
        self._conf_file = None
        self._serializer = _YAMLSerializer()

        if conf_file_path:
            self.load(conf_file_path)

    def generate(self):
        """
        Creates the LCA activities and parameters as defined in the configuration file.
        Also gets the LCIA methods defined in the conf file.
        :return: model: the top-level activity corresponding to the functional unit
        :return: methods: the LCIA methods
        """

        # Reset project for fresh start?
        reset = self._serializer.data.get(KEY_RESET, False)

        # Create model from configuration file
        project_name, model = self._build_model(reset=reset)

        # Get LCIA methods if declared
        methods = [resolve_method(eval(m)) for m in self._serializer.data.get(KEY_METHODS, [])]
        custom_methods = [eval(m.get(KEY_NAME)) for m in self._serializer.data.get(KEY_CUSTOM_METHODS, [])]
        methods.extend(custom_methods)

        # also return list of parameters?

        return project_name, model, methods

    def load(self, conf_file):
        """
        Reads the problem definition

        :param conf_file: Path to the file to open or a file descriptor
        """

        self._conf_file = pth.abspath(conf_file)  # for resolving relative paths

        self._serializer = _YAMLSerializer()
        self._serializer.read(self._conf_file)

        # Syntax validation
        with open_text(resources, JSON_SCHEMA_NAME) as json_file:
            json_schema = json.loads(json_file.read())

        validate(self._serializer.data, json_schema)
        # Issue a simple warning for unknown keys at root level
        for key in self._serializer.data:
            if key not in json_schema["properties"].keys():
                _LOGGER.warning(f"Configuration file: {key} is not a key declared in lca_modeller.")

    def _setup_premise(self, new_premise_scenarios: List[Dict], db_names: List[str] = None):
        """
        Generates the prospective databases with premise
        """
        if not new_premise_scenarios:
            return
        else:
            scenario_names = [
                f"{s[KEY_MODEL]}_{s[KEY_PATHWAY]}_{s[KEY_YEAR]}"
                for s in new_premise_scenarios
            ]
            _LOGGER.info(
                "Generating %d prospective premise database(s): %s",
                len(scenario_names),
                ", ".join(scenario_names),
            )

        if "biosphere3" not in bw2data.databases:
            raise ValueError(
                f"Biosphere database must be named 'biosphere3' for premise, or is missing. "
                f"Consider resetting the project with 'reset_project=True' in configuration file."
            )
        pm.clear_cache()  # fresh start
        ndb = pm.NewDatabase(
            scenarios=new_premise_scenarios,
            source_type="brightway",
            source_db=self.source_ei_name,
            source_version=self.ei_version,
            system_model=self.ei_model,
            key='tUePmX_S5B8ieZkkM7WUU2CnO8SmShwmAeWK9x2rTFo=',
            quiet=True
        )
        premise_dict = self._serializer.data.get(KEY_PREMISE, dict())
        sectors_to_update = premise_dict.get(KEY_UPDATE_PREMISE)
        if sectors_to_update == 'all':
            ndb.update()
        elif isinstance(sectors_to_update, list):
            ndb.update(sectors_to_update)
        ndb.write_db_to_brightway(name=db_names)

    def _setup_project(self, reset: bool = False):
        """
        Sets the brightway2 project and import the databases
        """
        if self._serializer.data is None:
            raise RuntimeError("read configuration file first")

        ### Init the brightway2 project
        project_name = self._serializer.data.get(KEY_PROJECT)
        if reset and confirm_reset(project_name):
            safe_delete_brightway_project(project_name)
        bw2data.projects.set_current(project_name)
        ei_dict = self._serializer.data.get(KEY_ECOINVENT)
        ei_version = self.ei_version = ei_dict[KEY_VERSION]
        ei_model = self.ei_model = ei_dict[KEY_MODEL]
        self.source_ei_name = f"ecoinvent-{ei_version}-{ei_model}"  # store for future use
        premise_dict = self._serializer.data.get(KEY_PREMISE, dict())
        self.premise_scenarios = premise_dict.get(KEY_SCENARIOS, [])

        if self.source_ei_name in bw2data.databases and not reset:
            _LOGGER.info(
                "Ecoinvent database '%s' already available; skipping import.",
                self.source_ei_name,
            )

            _LOGGER.info(
                "Set reset_project: True in configuration file to rebuild the Brightway project."
            )

        else:  ### Import Ecoinvent DB
            # User must create a file named .env, that he will not share /commit, and contains the following :
            # ECOINVENT_LOGIN=<your_login>
            # ECOINVENT_PASSWORD=<your_password>
            load_dotenv()  # This load .env file that contains the credential for EcoInvent into os.environ
            if not os.getenv("ECOINVENT_LOGIN") or not os.getenv("ECOINVENT_PASSWORD"):
                raise RuntimeError(
                    "Missing Ecoinvent credentials. Please set them in a .env file in the root of the project. \n"
                    "The file should contain the following lines : \n"
                    "ECOINVENT_LOGIN=<your_login>\n"
                    "ECOINVENT_PASSWORD=<your_password>\n")

            # This downloads ecoinvent and installs biopshere + technosphere + LCIA methods
            _LOGGER.info(
                "Importing ecoinvent %s (%s)...",
                ei_version,
                ei_model,
            )
            bw2io.import_ecoinvent_release(
                version=ei_version,
                system_model=ei_model,
                biosphere_name=BIOSPHERE3_DB_NAME,  # <-- premise requires the biosphere to be named "biosphere3"
                username=os.environ["ECOINVENT_LOGIN"],  # Read for .env file
                password=os.environ["ECOINVENT_PASSWORD"],  # Read from .env file
                use_mp=True)

        ### Generate prospective databases with premise
        new_premise_scenarios = []
        db_names = []
        for scenario in self.premise_scenarios:
            model = scenario[KEY_MODEL]
            pathway = scenario[KEY_PATHWAY]
            year = scenario[KEY_YEAR]
            db_name = f"ecoinvent_{ei_model}_{ei_version.replace('3.9.1', '3.9')}_{model}_{pathway}_{year}"
            if db_name not in bw2data.databases:
                new_premise_scenarios.append(scenario)
                db_names.append(db_name)
        self._setup_premise(new_premise_scenarios, db_names)

        ### Create new LCIA methods provided by user
        custom_methods = self._serializer.data.get(KEY_CUSTOM_METHODS, [])
        # create/reset biosphere db dedicated to new flows (e.g. contrails)
        _reset_db_and_proxy(
            USER_BIOSPHERE_DB_NAME,
            foreground=False,
        )
        if custom_methods:
            # implement new lcia methods
            _LOGGER.info(
                "Creating %d custom LCIA method(s)...",
                len(custom_methods),
            )
            for method in custom_methods:
                name = eval(method.get(KEY_NAME))
                filepath = method.get(KEY_FILEPATH)
                unit = method.get(KEY_UNIT)
                source_method = eval(method.get(KEY_SOURCE_METHOD)) if method.get(KEY_SOURCE_METHOD) else None
                create_custom_lcia_method(name, filepath, unit, source_method)

        ### Set the foreground database
        _reset_db_and_proxy(USER_DB, foreground=True)  # cleanup the whole foreground model to avoid errors
        agb.resetParams()  # reset parameters stored at project level (fresh start)
        # agb.Settings.factorize_static_bg = True  # improves performance on large model copying big Background activities
        # TODO: fix behaviour of factorize_static_bg (creates a foreground DB-proxy but needs
        #  to be handled correctly in particular regarding its reset (or not) at each run of the model.)

        return project_name

    def _build_model(self, reset: bool = False):
        """
        Builds the LCA model as defined in the configuration file.
        """

        ### Set up the project
        project_name = self._setup_project(reset=reset)

        ### Get parameters metadata if declared (used for parameters definition during the model creation)
        params_meta_array = self._serializer.data.get(KEY_METADATA)  # List of dictionaries (one per parameter)
        if params_meta_array:
            self.params_meta_dict = {param_metadata.get(KEY_PARAMETER): param_metadata for param_metadata in params_meta_array}
        else:
            self.params_meta_dict = {}

        ### Build the model
        _LOGGER.info("Building foreground model...")
        # Get model definition from configuration file
        model_definition = self._serializer.data.get(KEY_MODEL)
        model = agb.newActivity(
            db_name=USER_DB,
            name=KEY_MODEL,
            unit=None
        )

        self._parse_problem_table(model, model_definition)

        # Functional unit scaling
        # if KEY_FUNCTIONAL_VALUE in model_definition and model_definition[KEY_FUNCTIONAL_VALUE] != 1.0:
        #   # create a normalized model whose exchange with the model is equal to the functional value
        #   model_definition[KEY_EXCHANGE] = model_definition[KEY_FUNCTIONAL_VALUE]
        #    functional_value = self._parse_exchange(model_definition)
        #    normalized_model = agb.newActivity(
        #        db_name=USER_DB,
        #        name=KEY_NORMALIZED_MODEL,
        #        unit=None,
        #        exchanges={model: functional_value}
        #    )
        #    problem.model = normalized_model
        _LOGGER.info("Foreground model successfully created.")

        return project_name, model

    def _get_bio_activity(self, name, loc, categories, unit):
        """
        Searches for a biosphere activity in the default biosphere database.
        If not found, searches in the user biosphere database, which consists of unconventional biosphere flows defined
        by new LCIA methods (see newLCIAMethod()).
        :param name: name of the biosphere flow to search
        :param loc: geographical location
        :param categories: list of categories, e.g. ('air', 'lower stratosphere + upper troposphere')
        :param unit: unit of the biosphere flow (e.g. 'kilogram')
        :return:
        """
        sub_act = agb.findActivity(
            name=name,
            loc=loc,
            categories=categories,
            unit=unit,
            db_name=BIOSPHERE3_DB_NAME,
            single=False
        )
        if not sub_act:
            sub_act = agb.findActivity(
                name=name,
                loc=loc,
                categories=categories,
                unit=unit,
                db_name=USER_BIOSPHERE_DB_NAME,
            )
        return sub_act

    def _get_tech_activity(self, name, loc, unit, code: str = None, copy_act: bool = True):
        """
        Searches for an ecoinvent activity (or a previously defined one if '#' is prefixes the name).
        The activity is then copied to the foreground database to avoid any modification of the background database.
        :param name: name of the activity to search
        :param loc: geographical location
        :param unit: unit of activity
        :param code: new name to give to activity when it will be copied to the foreground database
        :return: a copy of the activity in the foreground database
        """

        # Previously defined foreground activity
        if name.startswith('#'):
            activity_name = name[1:]
            act = _get_foreground_activity(activity_name)

            if act is None:
                raise ValueError(
                    f"Activity with name '{activity_name}' not found "
                    f"in foreground database '{USER_DB}'."
                )

            return act

        # Background activity
        # Check if not already defined as a copy in the foreground database
        act = agb.findActivity(name=name, loc=loc, unit=unit, db_name=USER_DB, single=False)
        if not act:  # If not, get it from the background database and copy it to the foreground database
            act = agb.findActivity(
                name=name,
                loc=loc,
                unit=unit,
                db_name=self.source_ei_name
            )
            # Copy activity to foreground database so that we can safely modify it in the future
            if copy_act:
                _LOGGER.debug(
                    "Copying activity '%s' [%s] to foreground as '%s'.",
                    name,
                    loc,
                    code,
                )
                act = agb.copyActivity(
                    USER_DB,
                    act,
                    code
                )

        return act

    def _get_tech_activity_premise(self, name, loc, unit, copy_act: bool = True):
        """
        Searches for an ecoinvent activity in all databases generated by premise.
        The activities are copied in the foreground database to avoid any modification of the background databases.
        :param name: name of the activity to search
        :param loc: geographical location
        :param unit: unit of activity
        :return: a dict of (model, pathway, year): activity in the corresponding database
        """
        if not self.premise_scenarios:
            return []

        acts = {}
        for scenario in self.premise_scenarios:
            model = scenario[KEY_MODEL]
            pathway = scenario[KEY_PATHWAY]
            year = scenario[KEY_YEAR]
            db_name = f"ecoinvent_{self.ei_model}_{self.ei_version.replace('3.9.1', '3.9')}_{model}_{pathway}_{year}"
            code = name + f"\n[{loc}]\n({model}_{pathway.replace('-', '_')}_{year})" if loc else name + f"\n({model}_{pathway.replace('-', '_')}_{year})"

            # Check if activity not already defined as a copy in the foreground database
            act = agb.findActivity(name=code, loc=loc, unit=unit, db_name=USER_DB, single=False)
            if not act:  # If not, get it from the background database
                act = agb.findActivity(  # This is the activity for a given model, pathway and year
                        name=name,
                        loc=loc,
                        unit=unit,
                        db_name=db_name
                    )
                # default behaviour: copy it to the foreground database to avoid unwanted modification of background db
                if copy_act:
                    _LOGGER.debug(
                        "Copying premise activity '%s' (%s/%s/%s) to foreground.",
                        name,
                        model,
                        pathway,
                        year,
                    )
                    act = agb.copyActivity(  # safe copy of background act
                        db_name=USER_DB,
                        activity=act,
                        code=code  #name + f"\n({model}_{pathway.replace('-', '_')}_{year})"
                    )
            acts[(model, pathway.replace('-', '_'), year)] = act
        return acts

    def _create_proxy_activity_premise(self, name, loc, unit, code, modify_inventory=False):
        """
        Searches for an ecoinvent activity in the prospective databases and build a parent activity
        that enables to switch between scenarios with dedicated parameters.
        :param name: name of the activity to search
        :param loc: geographical location
        :param unit: unit of activity
        :param code: new name to give to activity when it will be copied to the foreground database
        :return: a multiswitch activity that points to the activity in the different prospective databases
        """

        # previously defined foreground activity --> not in premise. Regular search.
        if name.startswith('#'):
            return self._get_tech_activity(name, loc, unit)

        # Create parameters for year, model and pathway
        years = list(set([scenario[KEY_YEAR] for scenario in self.premise_scenarios]))
        models = list(set([scenario[KEY_MODEL] for scenario in self.premise_scenarios]))
        pathways = list(set([scenario[KEY_PATHWAY].replace('-', '_') for scenario in self.premise_scenarios]))
        # nb: replaced '-' by '_' in pathway names to avoid issues with lca parameters definition
        year_param = agb.params.all_params().get(
            KEY_YEAR,  # get parameter if already defined
            agb.newFloatParam(  # create float parameter if not already exists
                name=KEY_YEAR,
                default=years[0],
                min=min(years),
                max=max(years),
                dbname=USER_DB
            )
        )
        model_param = agb.params.all_params().get(
            KEY_MODEL,  # get switch parameter if already defined
            agb.newEnumParam(  # create switch parameter if not already exists
                name=KEY_MODEL,
                values=models,
                default=models[0],
                dbname=USER_DB
            )
        )
        pathway_param = agb.params.all_params().get(
            KEY_PATHWAY,  # get switch parameter if already defined
            agb.newEnumParam(  # create switch parameter if not already exists
                name=KEY_PATHWAY,
                values=pathways,
                default=pathways[0],
                dbname=USER_DB
            )
        )

        # Get the ecoinvent activity for each combination of model, pathway, and year
        acts = self._get_tech_activity_premise(
            name,
            loc,
            unit,
            copy_act=modify_inventory,
        )

        _LOGGER.debug(
            "Building prospective proxy '%s' from %d scenario activities.",
            code,
            len(acts),
        )

        # Dictionary to hold the lists of years for each (model, pathway)
        model_pathway_years = {}
        for model, pathway, year in acts.keys():
            model_pathway = (model, pathway)
            if model_pathway not in model_pathway_years:
                model_pathway_years[model_pathway] = []
            model_pathway_years[model_pathway].append(year)

        # Dictionary to hold the activities that point to the different years for each (model, pathway)
        acts_dict = {}
        for model_pathway, years in model_pathway_years.items():
            model = model_pathway[0]
            pathway = model_pathway[1]
            if len(years) == 1:
                acts_dict[model_pathway] = acts[(model, pathway, years[0])]
            else:  # create intermediate activity that is a linear interpolation between years
                _LOGGER.debug(
                    "Creating interpolation activity for '%s' (%s/%s) across years %s.",
                    name,
                    model,
                    pathway,
                    sorted(years),
                )
                acts_dict[model_pathway] = interpolate_activities(
                    db_name=USER_DB,
                    act_name=name + f"\n[{loc}]\n({model}_{pathway})" if loc else name + f"\n({model}_{pathway})",
                    param=year_param,
                    act_per_value={
                        year: acts[(model, pathway, year)] for year in years
                    },
                )

        # Create parent activity that enables to switch between each (model, pathway)
        _LOGGER.debug(
            "Creating premise switch activity '%s' for models=%s, pathways=%s.",
            code,
            models,
            pathways,
        )
        act = newMultiSwitchAct(
            dbname=USER_DB,
            name=code,
            paramDefList=[model_param, pathway_param],
            acts_dict=acts_dict
        )

        return act

    def _parse_problem_table(self, group, table: dict, group_switch_param=None):
        """
        Feeds provided group, using definition in provided table.

        :param group:
        :param table:
        """

        ### CASE 1: the table contains a single activity
        if KEY_NAME in table:
            name = table[KEY_NAME]
            loc = table.get(KEY_LOCATION, None)
            unit = table.get(KEY_UNIT, None)
            categories = table.get(KEY_CATEGORIES, None)
            exchange = _parse_exchange(table, params_meta_dict=self.params_meta_dict)
            custom_attributes = table.get(KEY_CUSTOM_ATTR, [])
            tags = table.get(KEY_TAGS, [])
            update_exchanges = table.get(KEY_UPDATE_ACT, [])
            delete_exchanges = table.get(KEY_DELETE, [])
            add_exchanges = table.get(KEY_ADD, [])

            # Biosphere flow
            if categories:
                sub_act = self._get_bio_activity(name, loc, categories, unit)
                if custom_attributes or tags:
                    _LOGGER.warning(
                        f"Custom attributes cannot apply directly to biosphere flows ({name}). Creating intermediate activity.")
                    sub_act = agb.newActivity(
                        db_name=USER_DB,
                        name=name,
                        unit=unit,
                        exchanges={sub_act: 1.0},
                    )
                    # Fix for mismatch chemical formulas (until fixed by future brightway/lca-algebraic releases)
                    for ex in sub_act.exchanges():
                        if "formula" in ex:
                            del ex["formula"]
                            ex.save()
                    # Both custom attributes and tags can be used by the user e.g. to split impacts by phase or system
                    for attr in custom_attributes:
                        attr_dict = {attr.get(KEY_ATTR_NAME): attr.get(KEY_ATTR_VALUE)}
                        sub_act.updateMeta(**attr_dict)
                    for tag in tags:
                        sub_act.updateMeta(**tag)

            # Technosphere activity
            else:
                modify_inventory = bool(
                    add_exchanges
                    or update_exchanges
                    or delete_exchanges
                )

                needs_wrapper = bool(
                    custom_attributes
                    or tags
                )

                if not self.premise_scenarios:

                    # CASE A: Activity inventory must be modified
                    if modify_inventory:
                        _LOGGER.debug(
                            "Activity '%s' has inventory modifications; using a foreground copy.",
                            name,
                        )
                        sub_act = self._get_tech_activity(
                            name=name,
                            loc=loc,
                            unit=unit,
                            copy_act=True,
                        )

                        # Metadata can safely be attached to the foreground copy
                        for attr in custom_attributes:
                            attr_dict = {
                                attr.get(KEY_ATTR_NAME):
                                    attr.get(KEY_ATTR_VALUE)
                            }
                            sub_act.updateMeta(**attr_dict)

                        for tag in tags:
                            sub_act.updateMeta(**tag)

                    else:
                        # CASE B/C: Keep original activity in background
                        sub_act = self._get_tech_activity(
                            name=name,
                            loc=loc,
                            unit=unit,
                            copy_act=False,
                        )

                        # Metadata only -> lightweight foreground wrapper
                        if needs_wrapper:
                            _LOGGER.debug(
                                "Creating lightweight foreground wrapper for activity '%s'.",
                                name,
                            )
                            sub_act = _create_activity_wrapper(
                                activity=sub_act,
                                name=name,
                                custom_attributes=custom_attributes,
                                tags=tags,
                            )

                else:  # premise databases
                    # Create parent activity that enables to switch between each (model, pathway and year)
                    sub_act = self._create_proxy_activity_premise(
                        name=name,
                        loc=loc,
                        unit=unit,
                        code=name,
                        modify_inventory=modify_inventory,
                    )

                    # Existing premise proxy is foreground, so metadata can be added
                    for attr in custom_attributes:
                        attr_dict = {
                            attr.get(KEY_ATTR_NAME):
                                attr.get(KEY_ATTR_VALUE)
                        }
                        sub_act.updateMeta(**attr_dict)

                    for tag in tags:
                        sub_act.updateMeta(**tag)

                # Modify exchanges only after ensuring we have a foreground copy
                if add_exchanges:
                    act_meta = {
                        KEY_NAME: name,
                        KEY_LOCATION: loc,
                        KEY_UNIT: unit,
                    }
                    self._add_exchanges(
                        sub_act,
                        act_meta,
                        add_exchanges,
                    )

                if update_exchanges:
                    act_meta = {
                        KEY_NAME: name,
                        KEY_LOCATION: loc,
                        KEY_UNIT: unit,
                    }
                    self._update_exchanges(
                        sub_act,
                        act_meta,
                        update_exchanges,
                    )

                if delete_exchanges:
                    act_meta = {
                        KEY_NAME: name,
                        KEY_LOCATION: loc,
                        KEY_UNIT: unit,
                    }
                    self._delete_exchanges(
                        sub_act,
                        act_meta,
                        delete_exchanges,
                    )

            group.addExchanges({sub_act: exchange})

        ### CASE 2: the table contains multiple activities
        for key, value in table.items():
            if isinstance(value, dict):  # value defines a sub activity
                # Check if an activity with this key as already been defined to avoid overriding it
                if _foreground_activity_exists(key):
                    _LOGGER.warning(
                        f"Activity with name '{key}' defined multiple times. "
                        f"Adding suffix increments to labels. "
                        f"To refer a pre-existing activity, use `name: '#activity_name'`."
                    )
                    key = _get_unique_activity_name(key)

                name = value.get(KEY_NAME, '')
                if name:  # It is a background activity (or a previously defined activity)
                    loc = value.get(KEY_LOCATION, None)
                    unit = value.get(KEY_UNIT, None)
                    categories = value.get(KEY_CATEGORIES, None)
                    exchange = _parse_exchange(value, params_meta_dict=self.params_meta_dict)
                    custom_attributes = value.get(KEY_CUSTOM_ATTR, [])
                    tags = value.get(KEY_TAGS, [])
                    update_exchanges = value.get(KEY_UPDATE_ACT, [])
                    delete_exchanges = value.get(KEY_DELETE, [])
                    add_exchanges = value.get(KEY_ADD, [])

                    # Biosphere flow
                    if categories:
                        sub_act = self._get_bio_activity(name, loc, categories, unit)
                        if custom_attributes or tags:
                            _LOGGER.warning(f"Custom attributes cannot apply directly to biosphere flows ({key}). Creating intermediate activity.")
                            sub_act = agb.newActivity(
                                db_name=USER_DB,
                                name=key,
                                unit=unit,
                                exchanges={sub_act: 1.0},
                            )
                            # Fix for mismatch chemical formulas (until fixed by future brightway/lca-algebraic releases)
                            for ex in sub_act.exchanges():
                                if "formula" in ex:
                                    del ex["formula"]
                                    ex.save()
                            for attr in custom_attributes:
                                attr_dict = {attr.get(KEY_ATTR_NAME): attr.get(KEY_ATTR_VALUE)}
                                sub_act.updateMeta(**attr_dict)
                            for tag in tags:
                                sub_act.updateMeta(**tag)

                    # Technosphere activity
                    else:
                        # A full foreground copy is only necessary if we modify
                        # the inventory of the background activity.
                        modify_inventory = bool(
                            update_exchanges
                            or delete_exchanges
                            or add_exchanges
                        )

                        # Tags/custom attributes only require a lightweight
                        # foreground wrapper, not a copy of the whole inventory.
                        needs_wrapper = bool(
                            custom_attributes
                            or tags
                        )

                        if not self.premise_scenarios:

                            # Get either a foreground copy (if inventory will be modified)
                            # or the untouched background activity.
                            sub_act = self._get_tech_activity(
                                name=name,
                                loc=loc,
                                unit=unit,
                                code=key,
                                copy_act=modify_inventory,
                            )

                            if modify_inventory:
                                _LOGGER.debug(
                                    "Activity '%s' has inventory modifications; using a foreground copy.",
                                    name,
                                )
                                # sub_act is now a foreground copy, so metadata can
                                # safely be attached directly to it.
                                for attr in custom_attributes:
                                    attr_dict = {
                                        attr.get(KEY_ATTR_NAME):
                                            attr.get(KEY_ATTR_VALUE)
                                    }
                                    sub_act.updateMeta(**attr_dict)

                                for tag in tags:
                                    sub_act.updateMeta(**tag)

                            elif needs_wrapper:
                                # sub_act is still the original background activity.
                                # Create a lightweight foreground activity only to
                                # carry tags/custom attributes.
                                _LOGGER.debug(
                                    "Creating lightweight foreground wrapper '%s' for background activity '%s'.",
                                    key,
                                    name,
                                )
                                sub_act = _create_activity_wrapper(
                                    activity=sub_act,
                                    name=key,
                                    custom_attributes=custom_attributes,
                                    tags=tags,
                                )

                        else:
                            # Create parent activity that enables to switch between each (model, pathway and year)
                            sub_act = self._create_proxy_activity_premise(
                                name=name,
                                loc=loc,
                                unit=unit,
                                code=key,
                                modify_inventory=modify_inventory,
                            )

                            # Premise proxy is already foreground, so metadata
                            # can be attached directly.
                            for attr in custom_attributes:
                                attr_dict = {
                                    attr.get(KEY_ATTR_NAME):
                                        attr.get(KEY_ATTR_VALUE)
                                }
                                sub_act.updateMeta(**attr_dict)

                            for tag in tags:
                                sub_act.updateMeta(**tag)

                        # At this point, if inventory modifications were requested,
                        # sub_act is guaranteed to be a foreground activity/copy.

                        if add_exchanges:
                            act_meta = {
                                KEY_NAME: name,
                                KEY_LOCATION: loc,
                                KEY_UNIT: unit,
                            }
                            self._add_exchanges(
                                sub_act,
                                act_meta,
                                add_exchanges,
                            )

                        if update_exchanges:
                            act_meta = {
                                KEY_NAME: name,
                                KEY_LOCATION: loc,
                                KEY_UNIT: unit,
                            }
                            self._update_exchanges(
                                sub_act,
                                act_meta,
                                update_exchanges,
                            )

                        if delete_exchanges:
                            act_meta = {
                                KEY_NAME: name,
                                KEY_LOCATION: loc,
                                KEY_UNIT: unit,
                            }
                            self._delete_exchanges(
                                sub_act,
                                act_meta,
                                delete_exchanges,
                            )

                    if group_switch_param:
                        # Parent group is a switch activity
                        switch_value = key.replace('_',
                                                   '')  # trick since lca algebraic does not handles correctly switch values with underscores
                        group.addExchanges({sub_act: exchange * group_switch_param.symbol(switch_value)})
                    else:
                        # Parent group is a regular activity
                        group.addExchanges({sub_act: exchange})
                else:
                    # It is a group
                    exchange = _parse_exchange(value, params_meta_dict=self.params_meta_dict)  # exchange with parent group
                    unit = value.get(KEY_UNIT, None)  # activity unit
                    is_switch = value.get(KEY_SWITCH, None)
                    switch_param = None
                    if not is_switch:
                        # It is a regular activity
                        sub_act = agb.newActivity(
                            db_name=USER_DB,
                            name=key,
                            unit=unit
                        )
                        # Add custom attributes
                        for attr in value.get(KEY_CUSTOM_ATTR, []):
                            attr_dict = {attr.get(KEY_ATTR_NAME): attr.get(KEY_ATTR_VALUE)}
                            sub_act.updateMeta(**attr_dict)
                        # Add tags
                        for tag in value.get(KEY_TAGS, []):
                            sub_act.updateMeta(**tag)
                    else:
                        # It is a switch activity
                        switch_values = value.copy()
                        switch_values.pop(KEY_EXCHANGE, None)
                        switch_values.pop(KEY_SWITCH)
                        switch_values = list(
                            switch_values.keys())  # [val + '_enum' for val in list(switch_values.keys())]
                        switch_values = [val.replace('_', '') for val in
                                         switch_values]  # trick since lca algebraic does not handles correctly switch values with underscores
                        switch_param = agb.newEnumParam(
                            name=key + '_switch_param',  # name of switch parameter
                            values=switch_values,  # possible values
                            default=switch_values[0],  # default value
                            dbname=USER_DB  # parameter defined in foreground database
                        )
                        sub_act = agb.newSwitchAct(
                            dbname=USER_DB,
                            name=key,
                            paramDef=switch_param,
                            acts_dict={}
                        )
                        # Add custom attributes
                        for attr in value.get(KEY_CUSTOM_ATTR, []):
                            attr_dict = {attr.get(KEY_ATTR_NAME): attr.get(KEY_ATTR_VALUE)}
                            sub_act.updateMeta(**attr_dict)
                        # Add tags
                        for tag in value.get(KEY_TAGS, []):
                            sub_act.updateMeta(**tag)

                    # Check if parent group is a switch activity or a regular activity
                    if group_switch_param:
                        # Parent group is a switch activity
                        switch_value = key.replace('_',
                                                   '')  # trick since lca algebraic does not handles correctly switch values with underscores
                        group.addExchanges({sub_act: exchange * group_switch_param.symbol(switch_value)})
                    else:
                        # Parent group is a regular activity
                        group.addExchanges({sub_act: exchange})
                    self._parse_problem_table(sub_act, value, switch_param)


    def _update_exchanges(self, act, act_meta, update_exchanges):
        """
        Updates exchanges of an activity already defined (e.g. ecoinvent activity) with new values.
        :param act: activity to be updated
        :param act_meta: dictionary of (name, loc, unit) of the original activity in ecoinvent
        :param update_exchanges: exchanges of the activity to be updated
        :return:
        """

        if act_meta.get(KEY_NAME).startswith('#'):
            _LOGGER.warning(f"Updating exchanges for a previously defined activity is not supported.")

        exchanges_to_update = {}
        premise_exchanges_to_update = {}

        for update in update_exchanges:
            input_activity = update.get(KEY_INPUT_ACT)
            new_value = update.get(KEY_NEW_VAL)

            if not isinstance(new_value, dict):
                raise ValueError(f"Invalid format for 'new_value' in 'update'. Expected a dictionary.")

            # TODO: refactor to allow sum() formula in new amount. See _add_exchanges() for example
            if KEY_EXCHANGE in new_value and len(new_value) == 1:  # update the amount only
                new_amount = _parse_exchange(new_value, params_meta_dict=self.params_meta_dict)
                new_input = None
            elif KEY_EXCHANGE not in new_value:  # update the input activity only
                new_amount = None
                new_input = self._get_new_input(new_value)
            else:  # update both amount and input activity
                new_amount = _parse_exchange(new_value, params_meta_dict=self.params_meta_dict)
                new_input = self._get_new_input(new_value)

            if not self.premise_scenarios:
                if new_input is None:
                    exchanges_to_update[input_activity] = dict(amount=new_amount)
                elif new_amount is None:
                    exchanges_to_update[input_activity] = dict(input=new_input)
                else:
                    exchanges_to_update[input_activity] = dict(amount=new_amount, input=new_input)

            else:
                if isinstance(new_input, dict):
                    for key in new_input.keys():
                        if key not in premise_exchanges_to_update:
                            premise_exchanges_to_update[key] = {}
                        if new_amount is None:
                            premise_exchanges_to_update[key][input_activity] = dict(input=new_input[key])
                        else:
                            premise_exchanges_to_update[key][input_activity] = dict(amount=new_amount, input=new_input[key])
                else:
                    if 'default' not in premise_exchanges_to_update:
                        premise_exchanges_to_update['default'] = {}
                    if new_input is None:
                        premise_exchanges_to_update['default'][input_activity] = dict(amount=new_amount)
                    elif new_amount is None:
                        premise_exchanges_to_update['default'][input_activity] = dict(input=new_input)
                    else:
                        premise_exchanges_to_update['default'][input_activity] = dict(amount=new_amount, input=new_input)

        if not self.premise_scenarios:
            act.updateExchanges(exchanges_to_update)
        else:
            self._update_multiple_databases(act_meta, premise_exchanges_to_update)

    def _delete_exchanges(self, act, act_meta, delete_exchanges):
        """
        Deletes exchanges of an activity already defined (e.g. ecoinvent activity).
        :param act: activity to be updated
        :param act_meta: dictionary of (name, loc, unit) of the original activity in ecoinvent
        :param delete_exchanges: exchanges of the activity to be deleted
        :return:
        """

        if act_meta.get(KEY_NAME).startswith('#'):
            _LOGGER.warning(f"Deleting exchanges for a previously defined activity is not supported.")
            return

        acts_premise = self._get_tech_activity_premise(
            name=act_meta.get(KEY_NAME),
            loc=act_meta.get(KEY_LOCATION),
            unit=act_meta.get(KEY_UNIT)
        )

        for delete in delete_exchanges:
            input_activity = delete.get(KEY_INPUT_ACT)
            if not self.premise_scenarios:
                act.deleteExchanges(input_activity, single=False)
            else:
                for act_premise in acts_premise.values():
                    act_premise.deleteExchanges(input_activity, single=False)

    def _add_exchanges(self, act, act_meta, add_exchanges):
        """
        Adds exchanges to an activity already defined (e.g. ecoinvent activity).
        :param act: activity to be updated
        :param act_meta: dictionary of (name, loc, unit) of the original activity in ecoinvent
        :param add_exchanges: exchanges to be added to the activity
        :return:
        """

        if act_meta.get(KEY_NAME).startswith('#'):
            _LOGGER.warning(f"Adding exchanges for a previously defined activity is not supported.")
            return

        exchanges_to_add = {}
        premise_exchanges_to_add = {}
        acts_premise = self._get_tech_activity_premise(
            name=act_meta.get(KEY_NAME),
            loc=act_meta.get(KEY_LOCATION),
            unit=act_meta.get(KEY_UNIT)
        )

        for add in add_exchanges:
            new_input = self._get_new_input(add)

            if not self.premise_scenarios:
                new_amount = _parse_exchange(add, act=act, params_meta_dict=self.params_meta_dict)
                exchanges_to_add[new_input] = new_amount

            else:
                for key, act_premise in acts_premise.items():
                    if key not in premise_exchanges_to_add:
                        premise_exchanges_to_add[key] = {}
                    new_amount = _parse_exchange(add, act=act_premise, params_meta_dict=self.params_meta_dict)
                    if isinstance(new_input, dict):
                        premise_exchanges_to_add[key][new_input[key]] = new_amount
                    else:
                        premise_exchanges_to_add[key][new_input] = new_amount

        if not self.premise_scenarios:
            act.addExchanges(exchanges_to_add)
        else:
            self._add_multiple_databases(act_meta, premise_exchanges_to_add)

    def _get_new_input(self, new_value):
        name = new_value.get(KEY_NAME)
        loc = new_value.get(KEY_LOCATION)
        unit = new_value.get(KEY_UNIT)
        categories = new_value.get(KEY_CATEGORIES)

        if name:  # background activity or previously defined foreground activity

            if categories: # Biosphere flow
                return self._get_bio_activity(name, loc, categories, unit)

            else: # Technosphere activity
                if not self.premise_scenarios or name.startswith('#'):
                    return self._get_tech_activity(name, loc, unit, copy_act=False) # copy act would be a bit overkill here
                else:
                    return self._get_tech_activity_premise(name, loc, unit, copy_act=False)

        else:  # new foreground activity
            new_act = new_value.copy()
            if KEY_EXCHANGE in new_act:
                new_act.pop(KEY_EXCHANGE)
            if len(new_act) > 1:
                _LOGGER.warning("Multiple activities defined as new input activity in 'new_value' in 'update'. "
                                "Only the first one will be considered.")
            key, value = next(iter(new_act.items()))
            unit = value.get(KEY_UNIT)
            if value.get(KEY_SWITCH):
                _LOGGER.warning("'is_switch' cannot be used when declaring new input activity in 'update'. Skipping.")
            new_input = agb.newActivity(db_name=USER_DB, name=key, unit=unit)
            self._parse_problem_table(new_input, value)
            return new_input

    def _update_multiple_databases(self, act_meta, premise_exchanges_to_update):
        acts_premise = self._get_tech_activity_premise(
            name=act_meta.get(KEY_NAME),
            loc=act_meta.get(KEY_LOCATION),
            unit=act_meta.get(KEY_UNIT)
        )

        for key, exchanges in premise_exchanges_to_update.items():
            if key == 'default':
                for premise_key in acts_premise.keys():
                    acts_premise[premise_key].updateExchanges(exchanges)
            else:
                acts_premise[key].updateExchanges(exchanges)

    def _add_multiple_databases(self, act_meta, premise_exchanges_to_add):
        acts_premise = self._get_tech_activity_premise(
            name=act_meta.get(KEY_NAME),
            loc=act_meta.get(KEY_LOCATION),
            unit=act_meta.get(KEY_UNIT)
        )

        for key, exchanges in premise_exchanges_to_add.items():
            if key == 'default':
                for premise_key in acts_premise.keys():
                    acts_premise[premise_key].addExchanges(exchanges)
            else:
                acts_premise[key].addExchanges(exchanges)


class _IDictSerializer(ABC):
    """Interface for reading and writing dict-like data"""

    @property
    @abstractmethod
    def data(self) -> dict:
        """
        The data that have been read, or will be written.
        """

    @abstractmethod
    def read(self, file_path: str):
        """
        Reads data from provided file.
        :param file_path:
        """

    @abstractmethod
    def write(self, file_path: str):
        """
        Writes data to provided file.
        :param file_path:
        """


class _YAMLSerializer(_IDictSerializer):
    """YAML-format serializer."""

    def __init__(self):
        self._data = None

    @property
    def data(self):
        return self._data

    def read(self, file_path: str):
        yaml = YAML(typ="safe")
        with open(file_path) as yaml_file:
            self._data = yaml.load(yaml_file)

    def write(self, file_path: str):
        yaml = YAML()
        yaml.default_flow_style = False
        with open(file_path, "w") as file:
            yaml.dump(self._data, file)