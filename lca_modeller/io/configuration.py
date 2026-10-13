import json
import os
from importlib.resources import open_text
from abc import ABC, abstractmethod
import shutil
from uuid import uuid4

import bw2data
import bw2calc
import bw2io
from bw2io.importers import ExcelLCIAImporter, CSVLCIAImporter
from bw2io.strategies import link_iterable_by_fields, normalize_units
from bw2io.utils import activity_hash
from bw2data.backends.schema import ActivityDataset
from jsonschema import validate
import lca_algebraic as agb
from sympy import sympify
import logging
import os.path as pth
from ruamel.yaml import YAML
from dotenv import load_dotenv
import premise as pm

from lca_modeller.io import resources
from lca_modeller.io.foreground import ForegroundBuilder
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
KEY_FOREGROUND_MODIFICATIONS = 'foreground_modifications'


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


def _parse_exchange(table: dict, act: ActivityExtended = None, params_meta_dict: dict = None, dbname: str = USER_DB):
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
        _LOGGER.debug(f"Summing amounts from exchanges {selected_exchanges} for activity {act}.")
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
                dbname=dbname  # parameter belongs to the generated foreground database
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


def _ensure_user_biosphere():
    """Keep custom flow identifiers stable across methods and model generations."""
    database = bw2data.Database(USER_BIOSPHERE_DB_NAME)
    if USER_BIOSPHERE_DB_NAME not in bw2data.databases:
        database.register()
    return database


def _link_custom_lcia_flows(importer):
    """Reuse custom flows, inserting new nodes without a database-wide VACUUM."""
    database = _ensure_user_biosphere()
    normalize_units(importer.data)
    link_iterable_by_fields(
        importer.data, other=database, edge_kinds=["biosphere"],
        fields=("name", "categories"),
    )
    created = {}
    for method in importer.data:
        for cf in method["exchanges"]:
            if cf.get("input"):
                continue
            identity = activity_hash(cf, fields=("name", "categories"))
            if identity not in created:
                categories = cf["categories"]
                flow = database.new_activity(
                    code=uuid4().hex, name=cf["name"], categories=categories,
                    unit=cf["unit"],
                    type="resource" if categories[0] == "resource" else "emission",
                )
                flow.save()
                created[identity] = flow.id
            cf["input"] = created[identity]
    if created:
        database.metadata["number"] = len(database)
        database.process()
        _LOGGER.info("Added %d custom biosphere flow(s).", len(created))


def _lcia_flow_id(flow):
    """Normalize a CF identifier without looking up already normalized BW25 IDs."""
    if isinstance(flow, int):
        return flow
    return bw2data.get_node(key=tuple(flow)).id


def _lcia_flow_metadata(flow_ids):
    """Read source-flow metadata in batches for the matching spreadsheet."""
    flow_ids = list(set(flow_ids))
    result = {}
    # Stay below SQLite's parameter limit, including on older installations.
    for start in range(0, len(flow_ids), 800):
        query = (
            ActivityDataset.select(ActivityDataset.id, ActivityDataset.data)
            .where(ActivityDataset.id.in_(flow_ids[start:start + 800]))
            .dicts()
        )
        result.update((row["id"], row["data"]) for row in query)
    missing = set(flow_ids) - result.keys()
    if missing:
        raise ValueError(f"Source LCIA method references missing biosphere flows: {sorted(missing)}")
    return result


def create_custom_lcia_method(name: str, filepath: str, unit: str = None, source_method: str = None):
    """Create or update a custom LCIA method from CSV or Excel characterization factors."""
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
        source_method = resolve_method(source_method)
        source_method_unit = bw2data.Method(source_method).metadata.get("unit")
        if unit != source_method_unit:
            _LOGGER.warning(
                f"Unit `{unit}` provided for method {name} "
                f"is different from source method. "
                f"Overriding with source method unit `{source_method_unit}`."
            )
        unit = source_method_unit

    Importer = CSVLCIAImporter if file_extension == '.csv' else ExcelLCIAImporter
    importer = Importer(filepath, name, DEFAULT_DESC_LCIA, unit)
    importer.apply_strategies(verbose=False)
    if importer.statistics(print_stats=False)[2]:
        _link_custom_lcia_flows(importer)

    source_cfs = []
    if source_method:
        # Iterating Method resolves every flow with a separate SQL query.
        source_cfs = [
            (_lcia_flow_id(row[0]), *row[1:])
            for row in bw2data.Method(source_method).load()
        ]

    for definition in importer.data:
        custom_cfs = [
            (_lcia_flow_id(cf["input"]), cf["amount"])
            for cf in definition["exchanges"]
        ]
        overridden = {row[0] for row in custom_cfs}
        inherited = [row for row in source_cfs if row[0] not in overridden]
        if inherited:
            metadata = _lcia_flow_metadata(row[0] for row in inherited)
            for row in inherited:
                flow = metadata[row[0]]
                # The matching report shows the central value; the method keeps
                # the complete source CF, including uncertainty and location.
                amount = row[1].get("amount") if isinstance(row[1], dict) else row[1]
                definition["exchanges"].append({
                    "name": flow.get("name"), "categories": flow.get("categories"),
                    "amount": amount, "unit": flow.get("unit"),
                    "type": "biosphere", "input": row[0],
                })

        method = bw2data.Method(definition["name"])
        if method.name in bw2data.methods:
            del bw2data.methods[method.name]
        method.register(
            description=definition["description"],
            filename=definition["filename"], unit=definition["unit"],
        )
        # Method.write already processes the method; the importer processes it twice.
        method.write(custom_cfs + inherited)

    importer.statistics()
    if source_method:
        fp_source = bw2io.export.excel.write_lcia_matching(importer, file_name)
        fp_destination = os.path.abspath(file_name) + '_updated.xlsx'
        shutil.move(fp_source, fp_destination)
        _LOGGER.info("LCIA matching file written to: %s", fp_destination)


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
        self.foreground_modifications = self._serializer.data.get(
            KEY_FOREGROUND_MODIFICATIONS, "copy",
        )
        scenarios = self._serializer.data.get(KEY_PREMISE, {}).get(KEY_SCENARIOS, [])
        pairs = {(s[KEY_MODEL], s[KEY_PATHWAY]) for s in scenarios}
        if len(pairs) > 1:
            raise ValueError(
                "Premise scenarios must use a single (model, pathway) pair; "
                f"received {sorted(pairs)!r}."
            )
        years = [s[KEY_YEAR] for s in scenarios]
        if len(years) != len(set(years)):
            raise ValueError("Premise scenarios must declare each year only once.")
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
        if bw2data.projects.current != project_name:
            bw2data.projects.set_current(project_name)
        else:
            _LOGGER.info(
                "Brightway project '%s' already set as current.",
                project_name,
            )
        ei_dict = self._serializer.data.get(KEY_ECOINVENT)
        ei_version = self.ei_version = ei_dict[KEY_VERSION]
        ei_model = self.ei_model = ei_dict[KEY_MODEL]
        self.source_ei_name = f"ecoinvent-{ei_version}-{ei_model}"  # store for future use
        premise_dict = self._serializer.data.get(KEY_PREMISE, dict())
        self.premise_scenarios = premise_dict.get(KEY_SCENARIOS, [])

        if self.source_ei_name in bw2data.databases and not reset:
            _LOGGER.info(
                "Ecoinvent database '%s' already imported. To clear and re-import, use reset_project: True in configuration file.",
                self.source_ei_name,
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
        # Existing custom methods and foreground models refer to these flow IDs.
        _ensure_user_biosphere()
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
        agb.Settings.factorize_static_bg = False  # keep disabled while validating foreground strategies

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

    def _parse_problem_table(self, group, table: dict, group_switch_param=None):
        """Build the YAML graph with year-specific premise input resolution."""
        builder = ForegroundBuilder(self, _parse_exchange, USER_DB)
        builder.build(group, table)
        self._foreground_builder = builder


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