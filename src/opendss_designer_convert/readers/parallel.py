"""Turn parallel branches between the same two buses into a series chain.

Source models (CYME, Synergi) put several devices on one section: a line, a
switch, a fuse, a transformer or regulator. Written one-for-one they would all
sit in parallel between the section's two nodes in OpenDSS, so a closed switch
would short out the transformer beside it. This mixin chains them in series
with new intermediate buses. Transformers stay in parallel with each other,
and so do the single-phase units of a regulator bank.

Moved here from upstream DiTTo's CYME reader so the Synergi reader can use it.
"""

from collections import defaultdict

from gdm.distribution.components.distribution_regulator import DistributionRegulator
from gdm.distribution.components.distribution_transformer import DistributionTransformer
from infrasys import Component


class ParallelBranchSerializer:
    """Mixin for readers that hold a GDM ``DistributionSystem`` in ``self.system``."""

    def serialize_parallel_branches(self):
        """
        Detect parallel branches and serialize them by inserting new buses and copying components as needed.
        All non-transformer parallel branches are chained in series. Only transformers may remain in parallel.
        After serialization, validates that no non-transformer parallel edges remain and that parallel
        transformers do not overlap on phases.
        """

        parallel_components = self._get_parallel_components()

        for i, comps in enumerate(parallel_components):
            transformers, non_transformers = self._sort_parallel_components(comps)
            if transformers:
                self._serialize_parallel_with_transformers(i, transformers, non_transformers)
            else:
                self._serialize_parallel_without_transformers(i, non_transformers)

    def _clone_with_new_name(self, obj, new_name):
        values = {}
        for field in type(obj).model_fields:
            cur_val = getattr(obj, field)
            if field == "name":
                val = new_name
            elif field in ("uuid",):
                continue
            else:
                val = cur_val
            values[field] = val
        return type(obj).model_construct(**values)

    def _create_parallel_bus(self, base_bus, index, edge_index):
        new_bus = self._clone_with_new_name(base_bus, f"{base_bus.name}_{index}_{edge_index}")
        self.system.add_component(new_bus)
        return new_bus

    @staticmethod
    def _group_regulator_banks(components):
        """Group the parallel components into series "slots".

        Each component is its own slot, except that the single-phase units of a
        regulator bank (DistributionRegulators on different phases) share one
        slot: they sit side by side between the same two buses, one per phase.
        Chaining them in series would leave every phase but the first open.
        """
        slots = []
        bank = None
        for comp in components:
            if isinstance(comp, DistributionRegulator):
                phases = set(comp.winding_phases[0]) if comp.winding_phases else set()
                if bank is not None and not (phases & bank[1]):
                    bank[0].append(comp)
                    bank[1].update(phases)
                    continue
                bank = ([comp], set(phases))
                slots.append(bank[0])
            else:
                slots.append([comp])
        return slots

    @staticmethod
    def _set_slot_bus(slot, index, bus):
        for comp in slot:
            comp.buses[index] = bus

    def _serialize_parallel_with_transformers(self, edge_index, transformers, non_transformers):
        xfmr_primary_base = transformers[0].buses[0]
        xfmr_primary = self._clone_with_new_name(
            xfmr_primary_base, f"{xfmr_primary_base.name}_primary"
        )
        self.system.add_component(xfmr_primary)
        xfmr_secondary = transformers[0].buses[1]

        slots = self._group_regulator_banks(non_transformers)
        for j, slot in enumerate(slots):
            if j == 0:
                if len(slots) > 1:
                    new_bus = self._create_parallel_bus(slots[j + 1][0].buses[1], j, edge_index)
                    self._set_slot_bus(slot, 0, new_bus)
                self._set_slot_bus(slot, 1, xfmr_primary)
            elif j < len(slots) - 1:
                self._set_slot_bus(slot, 1, slots[j - 1][0].buses[0])
                new_bus = self._create_parallel_bus(slots[j + 1][0].buses[1], j, edge_index)
                self._set_slot_bus(slot, 0, new_bus)
            else:
                self._set_slot_bus(slot, 1, slots[j - 1][0].buses[0])

        for comp in transformers:
            comp.buses[0] = xfmr_primary
            comp.buses[1] = xfmr_secondary

    def _serialize_parallel_without_transformers(self, edge_index, non_transformers):
        slots = self._group_regulator_banks(non_transformers)
        # CYME places a regulator at the source end of its section (Location S
        # in the samples), so put regulator slots first, nearest the from-node.
        slots.sort(key=lambda slot: not isinstance(slot[0], DistributionRegulator))
        for j, slot in enumerate(slots[:-1]):
            new_bus = self._create_parallel_bus(slot[0].buses[1], j, edge_index)
            self._set_slot_bus(slot, 1, new_bus)
            self._set_slot_bus(slots[j + 1], 0, new_bus)

    def _get_parallel_edges(self):
        G = self.system.get_undirected_graph()
        edges = defaultdict(list)
        for u, v, key in G.edges(keys=True):
            edge_pair = tuple(sorted((u, v)))
            edges[edge_pair].append(key)
        return G, {edge: keys for edge, keys in edges.items() if len(keys) > 1}

    def _get_parallel_components(self) -> list[list[Component]]:
        G, parallel_edges = self._get_parallel_edges()

        # Transformers may stay in parallel, and so may the single-phase units
        # of a regulator bank (one per phase between the same two buses).
        parallel_ok = (DistributionTransformer, DistributionRegulator)
        parallel_edges = {
            edge: keys
            for edge, keys in parallel_edges.items()
            if not all(
                G.get_edge_data(edge[0], edge[1], key)["type"] in parallel_ok for key in keys
            )
        }

        parallel_components = list()
        for edge in parallel_edges:
            comps = list()
            for key in parallel_edges[edge]:
                edge_data = G.get_edge_data(edge[0], edge[1], parallel_edges[edge][key])
                comps.append(self.system.get_component(edge_data["type"], edge_data["name"]))
            parallel_components.append(comps)

        return parallel_components

    def _sort_parallel_components(
        self, comps: list[Component]
    ) -> tuple[list[DistributionTransformer], list[Component]]:
        name_counts = defaultdict(int)
        for comp in comps:
            name_counts[comp.name] += 1

        seen_names = defaultdict(int)
        renamed_components = []
        for comp in comps:
            if name_counts[comp.name] > 1:
                new_name = f"{comp.name}_{seen_names[comp.name]}"
                # Use model_construct to avoid validation before assign_bus_voltages()
                # At this point buses have placeholder voltages, so pydantic validation would fail
                values = {}
                for field in type(comp).model_fields:
                    cur_val = getattr(comp, field)
                    if field == "name":
                        val = new_name
                    elif field in ("uuid",):
                        continue
                    else:
                        val = cur_val
                    values[field] = val
                new_comp = type(comp).model_construct(**values)
                self.system.add_component(new_comp)
                self.system.remove_component(comp)
                renamed_components.append(new_comp)
                seen_names[comp.name] += 1
            else:
                renamed_components.append(comp)

        transformers = [c for c in renamed_components if isinstance(c, DistributionTransformer)]
        non_transformers = [
            c for c in renamed_components if not isinstance(c, DistributionTransformer)
        ]

        return transformers, non_transformers
