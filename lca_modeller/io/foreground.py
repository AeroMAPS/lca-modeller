"""Build small foreground inventories and coherent premise year subgraphs."""

from collections import defaultdict
from dataclasses import dataclass, field
import logging
import re

from bw2data.configuration import labels
import lca_algebraic as agb
from sympy import sympify

from lca_modeller.interpolation import interpolate_activities

_LOGGER = logging.getLogger(__name__)
_METADATA = {
    "name", "loc", "unit", "categories", "amount", "tags",
    "custom_attributes", "is_switch", "add", "update", "delete",
}


@dataclass
class PremiseBundle:
    """Year-specific activities, with an optional public interpolation."""
    per_year: dict
    public_activity: object = None


@dataclass
class _Definition:
    label: str
    table: dict
    path: tuple
    scope: object
    public: bool
    children: list = field(default_factory=list)
    result: object = None
    building: bool = False


@dataclass
class _Exchange:
    input: object
    amount: object
    name: str
    type: str
    data: dict


class ForegroundBuilder:
    """Translate YAML definitions using either copies or sparse corrections.

    Both strategies use the same virtual inventory and year resolution. Only
    materialization differs, allowing their results to be compared directly.
    """

    def __init__(self, configurator, parse_exchange, foreground_db):
        self.configurator = configurator
        self.parse_exchange = parse_exchange
        self.db = foreground_db
        self.strategy = configurator.foreground_modifications
        self.scenarios = configurator.premise_scenarios
        self.years = sorted(s["year"] for s in self.scenarios)
        self.definitions = {}
        self.by_table = {}
        self.references = []
        self.background = {}
        self.matches = defaultdict(dict)
        self.year_param = None
        self.scaled_inputs = {}

    def build(self, root, table):
        self._scan(table, "model input" if table.get("name") else "model", (), None, public=True)
        for label, scope in self.references:
            if label not in self.definitions:
                raise ValueError(f"Referenced activity '#{label}' is not declared.")
            definition = self.definitions[label]
            if scope != definition.scope:
                definition.public = True
        definition = self.by_table[id(table)]
        definition.result = root
        self._fill_group(root, definition, year=None)
        self._validate_matches()
        return root

    def _scan(self, table, label, path, scope, public=False, register=True):
        base_label = label
        suffix = 1
        if register:
            while label in self.definitions:
                label = f"{base_label}_{suffix}"
                suffix += 1
            if label != base_label:
                _LOGGER.warning(
                    "Activity label '%s' declared more than once; using '%s'.",
                    base_label, label,
                )
        modified = any(table.get(key) for key in ("add", "update", "delete"))
        local_scope = scope
        if modified and scope is None:
            local_scope = path
        definition = _Definition(
            label, table, path, local_scope,
            public or scope is None or bool(table.get("tags") or table.get("custom_attributes")),
        )
        self.by_table[id(table)] = definition
        if register:
            self.definitions[label] = definition
        name = table.get("name", "")
        if name.startswith("#"):
            self.references.append((name[1:], scope))
        if not name:
            for key, value in table.items():
                if key not in _METADATA and isinstance(value, dict):
                    child = self._scan(value, key, path + (key,), scope)
                    definition.children.append((key, child))
        for index, item in enumerate(table.get("add", [])):
            self._scan_input(item, definition, ("add", index))
        for index, item in enumerate(table.get("update", [])):
            self._scan_input(item["new_value"], definition, ("update", index))
        return definition

    def _scan_input(self, value, owner, operation):
        path = owner.path + operation
        scope = owner.scope if owner.scope is not None else owner.path
        if "name" in value:
            self._scan(value, f"{owner.label}__{operation[0]}_{operation[1]}",
                       path, scope, register=False)
        else:
            children = [(k, v) for k, v in value.items() if k != "amount"]
            if not children:  # amount-only update
                return
            if len(children) != 1 or not isinstance(children[0][1], dict):
                raise ValueError(
                    f"New input in '{owner.label}' must declare exactly one activity."
                )
            key, table = children[0]
            self._scan(table, key, path + (key,), scope)

    def _build(self, definition):
        if definition.result is not None:
            return definition.result
        if definition.building:
            raise ValueError(f"Cyclic YAML reference involving '{definition.label}'.")
        definition.building = True
        try:
            if definition.table.get("name"):
                result = self._build_activity(definition)
            elif self.years and definition.scope is not None:
                result = PremiseBundle({
                    year: self._build_group(definition, year) for year in self.years
                })
            else:
                result = self._build_group(definition, None)
            definition.result = result
            if isinstance(result, PremiseBundle) and definition.public:
                self._public(result, definition)
            return result
        finally:
            definition.building = False

    def _name(self, definition, year):
        return definition.label if year is None else f"{definition.label} ({year})"

    def _metadata(self, activity, table, year=None):
        # Tags and custom attributes belong to the public logical group.
        # Generated yearly inventories remain implementation details.
        if year is not None:
            return
        metadata = {}
        for attribute in table.get("custom_attributes", []):
            metadata[attribute["attribute"]] = attribute["value"]
        for tag in table.get("tags", []):
            metadata.update(tag)
        if metadata:
            activity.updateMeta(**metadata)

    def _build_group(self, definition, year):
        table = definition.table
        activity = agb.newActivity(self.db, self._name(definition, year), table.get("unit"))
        self._metadata(activity, table, year=year)
        self._fill_group(activity, definition, year)
        return activity

    def _fill_group(self, activity, definition, year):
        switch = None
        if definition.table.get("is_switch"):
            values = [key.replace("_", "") for key, _ in definition.children]
            if len(set(values)) != len(values):
                raise ValueError(f"Ambiguous switch values in '{definition.label}'.")
            switch = agb.newEnumParam(
                definition.label, values=values, default=values[0], dbname=self.db,
            )
            activity.updateMeta(switch=True)
        if definition.table.get("name"):
            target = self._build_activity(definition)
            activity.addExchanges({
                self._resolve(target, definition, year):
                    self._amount(definition.table),
            })
            return
        for key, child in definition.children:
            target = self._resolve(self._build(child), child, year)
            amount = self._amount(child.table)
            if switch is not None:
                amount *= switch.symbol(key.replace("_", ""))
            activity.addExchanges({target: amount})

    def _public(self, bundle, definition):
        if bundle.public_activity is None:
            if len(bundle.per_year) == 1:
                target = next(iter(bundle.per_year.values()))
                activity = agb.newActivity(
                    self.db, definition.label, target.get("unit"), exchanges={target: 1},
                )
            else:
                if self.year_param is None:
                    existing = agb.params.all_params().get("year")
                    self.year_param = existing if existing is not None else agb.newFloatParam(
                        "year", default=self.years[0], min=self.years[0],
                        max=self.years[-1], dbname=self.db,
                    )
                activity = interpolate_activities(
                    self.db, definition.label, self.year_param, bundle.per_year,
                )
            self._metadata(activity, definition.table)
            bundle.public_activity = activity
        return bundle.public_activity

    def _resolve(self, result, definition, year):
        if not isinstance(result, PremiseBundle):
            return result
        if year is not None:
            return result.per_year[year]
        return self._public(result, definition)

    def _database(self, year):
        if year is None:
            return self.configurator.source_ei_name
        scenario = next(s for s in self.scenarios if s["year"] == year)
        version = self.configurator.ei_version.replace("3.9.1", "3.9")
        return (
            f"ecoinvent_{self.configurator.ei_model}_{version}_"
            f"{scenario['model']}_{scenario['pathway']}_{year}"
        )

    def _find_background(self, table, year):
        database = self._database(year)
        key = (database, table["name"], table.get("loc"), table.get("unit"))
        if key not in self.background:
            self.background[key] = agb.findActivity(
                name=table["name"], loc=table.get("loc"), unit=table.get("unit"),
                db_name=database,
            )
        return self.background[key]

    def _build_activity(self, definition):
        table = definition.table
        name = table["name"]
        if name.startswith("#"):
            if any(table.get(k) for k in ("add", "update", "delete")):
                raise ValueError(
                    f"Inventory modifications on reference '{name}' are unsupported; "
                    "declare a new background activity to modify."
                )
            referenced = self.definitions[name[1:]]
            result = self._build(referenced)
            if table.get("tags") or table.get("custom_attributes"):
                if isinstance(result, PremiseBundle):
                    # The reference owns a tagged public group and shares the
                    # original yearly inputs without creating yearly wrappers.
                    return PremiseBundle(result.per_year.copy())
                return self._reference_wrapper(result, definition, None)
            return result
        if table.get("categories"):
            result = self.configurator._get_bio_activity(
                name, table.get("loc"), table.get("categories"), table.get("unit"),
            )
            if isinstance(result, list):
                if len(result) != 1:
                    raise ValueError(f"Biosphere flow '{name}' must resolve uniquely.")
                result = result[0]
            if table.get("tags") or table.get("custom_attributes"):
                result = self._reference_wrapper(result, definition, None)
                # A biosphere chemical formula is not an algebraic exchange formula.
                for exchange in result.exchanges():
                    if "formula" in exchange:
                        del exchange["formula"]
                        exchange.save()
            return result
        if self.years:
            return PremiseBundle({
                year: self._materialize(definition, year) for year in self.years
            })
        return self._materialize(definition, None)

    def _reference_wrapper(self, target, definition, year):
        activity = agb.newActivity(
            self.db, self._name(definition, year), target.get("unit"),
            exchanges={target: 1},
        )
        self._metadata(activity, definition.table, year=year)
        return activity

    def _inventory(self, source):
        inventory = []
        for exchange in source.exchanges():
            if exchange["input"] == source.key and exchange["type"] == labels.production_edge_default:
                continue
            data = exchange.as_dict().copy()
            # Background chemical formulas must never be interpreted as amounts.
            data.pop("formula", None)
            inventory.append(_Exchange(
                exchange.input, sympify(exchange["amount"]),
                data.get("name", exchange.input["name"]), exchange["type"], data,
            ))
        return inventory

    @staticmethod
    def _matching(inventory, pattern):
        name, separator, location = pattern.partition("#")
        wildcard = "*" in name
        name = name.replace("*", "")
        matches = []
        for exchange in inventory:
            if not (name in exchange.name if wildcard else name == exchange.name):
                continue
            if separator:
                actual_location = exchange.input.get("location")
                if actual_location is None:
                    continue
                if location.startswith("!"):
                    if actual_location == location[1:]:
                        continue
                elif actual_location != location:
                    continue
            matches.append(exchange)
        return matches

    def _select(self, inventory, pattern, definition, year, operation):
        matches = self._matching(inventory, pattern)
        if "*" in pattern:
            self.matches[(definition.path, operation, pattern)][year] = bool(matches)
        elif not matches:
            raise ValueError(
                f"No exchange matches '{pattern}' in '{definition.label}'"
                + (f" for year {year}." if year is not None else ".")
            )
        if operation[0] == "update" and "*" not in pattern and len(matches) > 1:
            raise ValueError(
                f"Exchange '{pattern}' is ambiguous in '{definition.label}'; use a wildcard."
            )
        return matches

    def _validate_matches(self):
        for (path, operation, pattern), matched in self.matches.items():
            missing = [year for year, found in matched.items() if not found]
            if not any(matched.values()):
                raise ValueError(
                    f"No exchange matches wildcard '{pattern}' in {'/'.join(map(str, path))} "
                    "for any configured year."
                )
            if missing:
                _LOGGER.warning(
                    "Wildcard '%s' matched no exchanges for years %s in %s (%s), "
                    "but matched in other years.",
                    pattern, missing, "/".join(map(str, path)), operation[0],
                )

    def _amount(self, table, inventory=None, definition=None, year=None, operation=None):
        amount = str(table.get("amount", 1.0))
        if inventory is not None:
            def substitute(match):
                selected = self._select(
                    inventory, match.group(1), definition, year, operation,
                )
                return f"({sum((e.amount for e in selected), sympify(0))})"
            amount = re.sub(r"\bsum\(([^()]*)\)", substitute, amount)
        return self.parse_exchange(
            {"amount": amount}, params_meta_dict=self.configurator.params_meta_dict,
            dbname=self.db,
        )

    def _input(self, value, year):
        if "name" in value:
            definition = self.by_table[id(value)]
        else:
            children = [(k, v) for k, v in value.items() if k != "amount"]
            if len(children) != 1:
                raise ValueError("A new input must declare exactly one activity.")
            definition = self.by_table[id(children[0][1])]
        target = self._resolve(self._build(definition), definition, year)
        # A nested named definition has its own amount inside the replacement.
        # The amount on a direct new_value belongs to the edited exchange.
        if "name" not in value and definition.table.get("amount", 1) != 1:
            key = (definition.path, year)
            if key not in self.scaled_inputs:
                self.scaled_inputs[key] = agb.newActivity(
                    self.db, self._name(definition, year) + " input", target.get("unit"),
                    exchanges={target: self._amount(definition.table)},
                )
            target = self.scaled_inputs[key]
        return target

    def _edited_inventory(self, definition, year, inventory):
        table = definition.table
        # Evaluate all additions against the inventory before adding exchanges,
        # as in the original builder. Subsequent updates/deletions see additions.
        additions = []
        for index, item in enumerate(table.get("add", [])):
            target = self._input(item, year)
            amount = self._amount(item, inventory, definition, year, ("sum", index))
            kind = (
                labels.consumption_edge_default
                if target.get("type") in labels.lci_node_types
                else labels.biosphere_edge_default
            )
            additions.append(_Exchange(
                target, amount, target["name"], kind,
                {"name": target["name"], "unit": target.get("unit"), "type": kind},
            ))
        inventory.extend(additions)
        for index, update in enumerate(table.get("update", [])):
            new_value = update["new_value"]
            selected = self._select(
                inventory, update["input_activity"], definition, year, ("update", index),
            )
            target = self._input(new_value, year) if any(k != "amount" for k in new_value) else None
            expression = (
                self._amount(new_value, inventory, definition, year, ("sum_update", index))
                if "amount" in new_value else None
            )
            for exchange in selected:
                if expression is not None:
                    exchange.amount = expression.subs(agb.old_amount, exchange.amount)
                if target is not None:
                    exchange.input = target
        for index, deletion in enumerate(table.get("delete", [])):
            selected = self._select(
                inventory, deletion["input_activity"], definition, year, ("delete", index),
            )
            selected_ids = {id(exchange) for exchange in selected}
            inventory[:] = [exchange for exchange in inventory if id(exchange) not in selected_ids]
        return inventory

    @staticmethod
    def _write_exchange(activity, exchange, source=None):
        data = exchange.data.copy()
        for key in ("input", "output", "amount", "formula"):
            data.pop(key, None)
        target = exchange.input
        if source is not None and target.key == source.key:
            target = activity
        data.update(input=target.key, name=exchange.name, type=exchange.type)
        created = activity.new_exchange(**data)
        amount = exchange.amount
        if not amount.free_symbols:
            amount = float(amount)
        activity._update_exchange(created, {"amount": amount})

    def _materialize(self, definition, year):
        source = self._find_background(definition.table, year)
        modified = any(definition.table.get(k) for k in ("add", "update", "delete"))
        if not modified:
            annotated = bool(
                definition.table.get("tags")
                or definition.table.get("custom_attributes")
            )
            if annotated and year is None:
                return self._reference_wrapper(source, definition, year)
            # A public premise group already carries the metadata. Its
            # interpolation can point directly to the background activities.
            return source
        original = self._inventory(source)
        inventory = self._edited_inventory(definition, year, self._inventory(source))
        output = source.getOutputAmount()
        production = [
            exchange for exchange in source.exchanges()
            if exchange["type"] == labels.production_edge_default
        ]
        eligible = (
            len(production) == 1 and production[0]["input"] == source.key
            and output != 0
            and all(e.input.key != source.key for e in original)
            and all(e.type in (
                labels.consumption_edge_default, labels.biosphere_edge_default,
            ) for e in original)
        )
        if self.strategy == "overlay" and eligible:
            activity = agb.newActivity(
                self.db, self._name(definition, year), source.get("unit"), amount=output,
                exchanges={source: output},
            )
            # Aggregate corrections by both input and exchange type so duplicate
            # inputs and biosphere exchanges retain their signs and semantics.
            deltas = {}
            for sign, exchanges in ((-1, original), (1, inventory)):
                for exchange in exchanges:
                    key = (exchange.input.key, exchange.type)
                    if key not in deltas:
                        deltas[key] = _Exchange(
                            exchange.input, sympify(0), exchange.name,
                            exchange.type, exchange.data,
                        )
                    deltas[key].amount += sign * exchange.amount
            for exchange in deltas.values():
                if exchange.amount != 0:
                    self._write_exchange(activity, exchange)
        else:
            if self.strategy == "overlay":
                _LOGGER.debug(
                    "Using copy for '%s' (%s): inventory has multiple outputs, "
                    "self-inputs, or exchange types unsuitable for an overlay.",
                    definition.label, year,
                )
            activity = agb.copyActivity(
                self.db, source, self._name(definition, year), withExchanges=False,
            )
            # Duplicate input edges must be combined: lca_algebraic's
            # static factorization otherwise retains only the last amount.
            combined = {}
            for exchange in inventory:
                key = (exchange.input.key, exchange.type)
                if key not in combined:
                    combined[key] = _Exchange(
                        exchange.input, sympify(0), exchange.name,
                        exchange.type, exchange.data,
                    )
                combined[key].amount += exchange.amount
            for exchange in combined.values():
                self._write_exchange(activity, exchange, source)
        self._metadata(activity, definition.table, year=year)
        return activity
