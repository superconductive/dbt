import dbt.exceptions

from dbt.utils import deep_merge
from dbt.node_types import NodeType
from dbt.adapters.factory import get_adapter_class_by_name


class SourceConfig(object):
    AppendListFields = {'pre-hook', 'post-hook', 'tags'}
    ExtendDictFields = {'vars', 'column_types', 'quoting', 'persist_docs'}
    ClobberFields = {
        'alias',
        'schema',
        'enabled',
        'materialized',
        'unique_key',
        'database',
        'severity',
        'docs',
        'incremental_strategy'
    }

    ConfigKeys = AppendListFields | ExtendDictFields | ClobberFields

    def __init__(self, active_project, own_project, fqn, node_type):
        self._config = None
        # active_project is a RuntimeConfig, not a Project
        self.active_project = active_project
        self.own_project = own_project
        self.fqn = fqn
        self.node_type = node_type

        adapter_type = active_project.credentials.type
        adapter_class = get_adapter_class_by_name(adapter_type)
        self.AdapterSpecificConfigs = adapter_class.AdapterSpecificConfigs

        # the config options defined within the model
        self.in_model_config = {}

    def _merge(self, *configs):
        merged_config = {}
        for config in configs:
            intermediary_merged = deep_merge(
                merged_config.copy(), config.copy()
            )

            merged_config.update(intermediary_merged)
        return merged_config

    # this is re-evaluated every time `config` is called.
    # we can cache it, but that complicates things.
    # TODO : see how this fares performance-wise
    @property
    def config(self):
        """
        Config resolution order:

         if this is a dependency model:
           - own project config
           - in-model config
           - active project config
         if this is a top-level model:
           - active project config
           - in-model config
        """

        defaults = {"enabled": True, "materialized": "view"}

        if self.node_type == NodeType.Seed:
            defaults['materialized'] = 'seed'
        elif self.node_type == NodeType.Snapshot:
            defaults['materialized'] = 'snapshot'

        if self.node_type == NodeType.Test:
            defaults['severity'] = 'ERROR'

        active_config = self.load_config_from_active_project()

        if self.active_project.project_name == self.own_project.project_name:
            cfg = self._merge(defaults, active_config,
                              self.in_model_config)
        else:
            own_config = self.load_config_from_own_project()

            cfg = self._merge(
                defaults, own_config, self.in_model_config, active_config
            )

        return cfg

    def _translate_adapter_aliases(self, config):
        return self.active_project.credentials.translate_aliases(config)

    def update_in_model_config(self, config):
        config = self._translate_adapter_aliases(config)
        for key, value in config.items():
            if key in self.AppendListFields:
                current = self.in_model_config.get(key, [])
                if not isinstance(value, (list, tuple)):
                    value = [value]
                current.extend(value)
                self.in_model_config[key] = current
            elif key in self.ExtendDictFields:
                current = self.in_model_config.get(key, {})
                try:
                    current.update(value)
                except (ValueError, TypeError, AttributeError):
                    dbt.exceptions.raise_compiler_error(
                        'Invalid config field: "{}" must be a dict'.format(key)
                    )
                self.in_model_config[key] = current
            else:  # key in self.ClobberFields or self.AdapterSpecificConfigs
                self.in_model_config[key] = value

    @staticmethod
    def __get_as_list(relevant_configs, key):
        if key not in relevant_configs:
            return []

        items = relevant_configs[key]
        if not isinstance(items, (list, tuple)):
            items = [items]

        return items

    def smart_update(self, mutable_config, new_configs):
        config_keys = self.ConfigKeys | self.AdapterSpecificConfigs

        relevant_configs = {
            key: new_configs[key] for key
            in new_configs if key in config_keys
        }

        for key in self.AppendListFields:
            append_fields = self.__get_as_list(relevant_configs, key)
            mutable_config[key].extend([
                f for f in append_fields if f not in mutable_config[key]
            ])

        for key in self.ExtendDictFields:
            dict_val = relevant_configs.get(key, {})
            try:
                mutable_config[key].update(dict_val)
            except (ValueError, TypeError, AttributeError):
                dbt.exceptions.raise_compiler_error(
                    'Invalid config field: "{}" must be a dict'.format(key)
                )

        for key in (self.ClobberFields | self.AdapterSpecificConfigs):
            if key in relevant_configs:
                mutable_config[key] = relevant_configs[key]

        return relevant_configs

    def get_project_config(self, runtime_config):
        # most configs are overwritten by a more specific config, but pre/post
        # hooks are appended!
        config = {}
        for k in self.AppendListFields:
            config[k] = []
        for k in self.ExtendDictFields:
            config[k] = {}

        if self.node_type == NodeType.Seed:
            model_configs = runtime_config.seeds
        elif self.node_type == NodeType.Snapshot:
            model_configs = {}
        else:
            model_configs = runtime_config.models

        if model_configs is None:
            return config

        # mutates config
        self.smart_update(config, model_configs)

        fqn = self.fqn[:]
        for level in fqn:
            level_config = model_configs.get(level, None)
            if level_config is None:
                break

            # mutates config
            relevant_configs = self.smart_update(config, level_config)

            clobber_configs = {
                k: v for (k, v) in relevant_configs.items()
                if k not in self.AppendListFields and
                k not in self.ExtendDictFields
            }

            config.update(clobber_configs)
            model_configs = model_configs[level]

        return config

    def load_config_from_own_project(self):
        return self.get_project_config(self.own_project)

    def load_config_from_active_project(self):
        return self.get_project_config(self.active_project)
