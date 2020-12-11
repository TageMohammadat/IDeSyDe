'''
The identification module provides all types/classes and methods
to perform DSI as specified in the papers that follows:

    [DSI-DATE2022]

It should _not_ depend in the exploration module, pragmatically
or conceptually, but only on other modules that provides model
utilities, like SDF analysis or SY analysis.
'''
import concurrent.futures
import importlib.resources as resources
import os
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import List, Union, Tuple, Set, Optional, Dict, Type, Iterable, Any

import numpy as np
import sympy
from minizinc import Model as MznModel
from minizinc import Instance as MznInstance
from minizinc import Result as MznResult
from forsyde.io.python import ForSyDeModel
from forsyde.io.python import Vertex
from forsyde.io.python import Edge
from forsyde.io.python import Port
from forsyde.io.python.types import TypesFactory

import idesyde.math as mathutil
import idesyde.sdf as sdfapi


class ChoiceCriteria(Flag):
    '''Flag to indicate decision model subsumption
    '''
    DOMINANCE = auto()


@dataclass
class DecisionModel(object):

    """Decision Models interface for the DSI procedure.

    A dict like interface is implemented for decision models
    for convenience, which recursively checks any other partial
    identifications that the model may have.
    """

    dominated_models: Set[Type["DecisionModel"]] = field(
        default_factory=lambda: set()
    )

    @classmethod
    def identify(
            cls,
            model: ForSyDeModel,
            subproblems: List["DecisionModel"]
    ) -> Tuple[bool, Optional["DecisionModel"]]:
        """Perform identification procedure and obtain a new Decision Model

        This class function analyses the given design model (ForSyDe Model)
        and returns a decision model which partially idenfity it. It
        indicates when it can still be executed or not via a tuple.

        Arguments:
            model: Input ForSyDe model.
            subproblems: Decision Models that have already been identified.

        Returns:
            A tuple where the first element indicates if any decision model
            belonging to 'cls' can still be identified and a decision model
            that partially identifies 'model' in the second element.
        """
        return (True, None)

    def __iter__(self):
        for k in self.__dict__:
            o = self.__dict__[k]
            if isinstance(o, DecisionModel):
                yield from o
            else:
                yield k

    def __getitem__(self, key):
        key = str(key)
        if key in self.__dict__:
            return self.__dict__[key]
        for k in self.__dict__:
            o = self.__dict__[k]
            if isinstance(o, DecisionModel) and key in o:
                return o[key]
        return KeyError

    def __contains__(self, key):
        key = str(key)
        if key in self.__dict__:
            return True
        for k in self.__dict__:
            o = self.__dict__[k]
            if isinstance(o, DecisionModel) and key in o:
                return True
        return False

    def compute_deduced_properties(self) -> None:
        '''Compute deducible properties for this decision model'''
        raise NotImplementedError

    def covered_vertexes(self) -> Iterable[Vertex]:
        '''Get vertexes partially identified by the Decision Model.

        Returns:
            Iterable for the vertexes.
        '''
        for o in self:
            if isinstance(o, Vertex):
                yield o

    def covered_edges(self) -> Iterable[Edge]:
        '''Get edges partially identified by the Decision Model.

        Returns:
            Iterable for the edges.
        '''
        for o in self:
            if isinstance(o, Edge):
                yield o

    def covered_model(self) -> ForSyDeModel:
        '''Returns the covered ForSyDe Model.
        Returns:
            A copy of the vertexes and edges that this decision
            model partially identified.
        '''
        model = ForSyDeModel()
        for v in self.covered_vertexes():
            model.add_node(v, label=v.identifier)
        for e in self.covered_edges():
            model.add_edge(
                e.source_vertex,
                e.target_vertex,
                data=e
            )
        return model

    def dominates(self, other: "DecisionModel") -> bool:
        '''
        This function returns if one partial identification dominates
        the other. It also takes in consideration the explicit
        model domination set from 'self'.

        Args:
            other: the other decision model to be checked.

        Returns:
            True if 'self' dominates other. False otherwise.
        '''
        if any(isinstance(other, t) for t in self.dominated_models):
            return True
        else:
            # other - self
            other_diff = set(
                k for k in other if k not in self
            )
            # self - other
            self_diff = set(
                k for k in self if k not in other
            )
            # other is fully contained in self and itersection is consistent
            return len(other_diff) == 0\
                and len(self_diff) >= 0
            # and all(self[k] == other[k] for k in other if k in self)


class MinizincAble(object):
    '''Interface that enables consumption by minizinc-based solvers.
    '''

    def get_mzn_data(self) -> Dict[str, Any]:
        '''Build the input minizinc dictionary

        As the minizinc library has a dict-like interface but
        is immutable once a value is set, this pre-filling diciotnary
        is passed aroudn before feeding the minizinc interface for
        better code reuse.

        Returns:
            A dictionary containing all the data necessary to run
            the minizinc model attached to this decision model.
        '''
        return dict()

    def populate_mzn_model(
            self,
            model: Union[MznModel, MznInstance]
    ) -> Union[MznModel, MznInstance]:
        data_dict = self.get_mzn_data()
        for k in data_dict:
            model[k] = data_dict[k]
        return model

    def get_mzn_model_name(self) -> str:
        '''Get the number of the minizinc file for this class.

        Returns:
            the name of the file that represents this decision model.
            Although a method, it is expected that the string return
            is constant, i.e. static.
        '''
        return ""

    def rebuild_forsyde_model(
        self,
        result: MznResult
    ) -> ForSyDeModel:
        return ForSyDeModel()

    def build_mzn_model(self, mzn=MznModel()):
        model_txt = resources.read_text(
            'idesyde.minizinc',
            self.get_mzn_model_name()
        )
        mzn.add_string(model_txt)
        self.populate_mzn_model(mzn)
        return mzn


@dataclass
class SDFExecution(DecisionModel):

    """
    This decision model captures all SDF actors and channels in
    the design model and can only be identified if the 'Global' SDF
    application (the union of all disjoint SDFs) is consistent, i.e.
    it has a PASS.

    After identification this decision model provides the global
    SDF topology and the PASS with all elements included.
    """

    sdf_actors: List[Vertex] = field(default_factory=list)
    sdf_channels: List[Vertex] = field(default_factory=list)
    sdf_topology: np.ndarray = np.zeros((0, 0))
    sdf_repetition_vector: np.ndarray = np.zeros((0))
    sdf_pass: List[str] = field(default_factory=list)

    @classmethod
    def identify(cls, model, identified):
        res = None
        sdf_actors = list(a for a in model.query_vertexes('sdf_actors'))
        sdf_channels = list(c for c in model.query_vertexes('sdf_channels'))
        sdf_topology = np.zeros(
            (len(sdf_channels), len(sdf_actors)),
            dtype=int
        )
        for row in model.query_view('sdf_topology'):
            a_index = next(idx for (idx, v) in enumerate(sdf_actors)
                           if v.identifier == row['actor_id'])
            c_index = next(idx for (idx, v) in enumerate(sdf_channels)
                           if v.identifier == row['channel_id'])
            sdf_topology[c_index, a_index] = int(row['tokens'])
        null_space = sympy.Matrix(sdf_topology).nullspace()
        if len(null_space) == 1:
            repetition_vector = mathutil.integralize_vector(null_space[0])
            repetition_vector = np.array(repetition_vector, dtype=int)
            initial_tokens = np.zeros((sdf_topology.shape[0], 1))
            schedule = sdfapi.get_PASS(sdf_topology,
                                       repetition_vector,
                                       initial_tokens)
            if schedule != []:
                sdf_pass = [sdf_actors[idx] for idx in schedule]
                res = SDFExecution(
                    sdf_actors,
                    sdf_channels,
                    sdf_topology,
                    repetition_vector,
                    sdf_pass
                )
        # conditions for fixpoints and partial identification
        if res:
            return (True, res)
        else:
            return (False, None)


@dataclass
class SDFToOrders(DecisionModel, MinizincAble):

    # sub identifications
    sdf_exec_sub: SDFExecution = SDFExecution()

    # partial identification
    orderings: List[Vertex] = field(default_factory=list)

    # deduced properties
    max_tokens: int = 0

    @classmethod
    def identify(cls, model, identified):
        res = None
        sdf_exec_sub = next(
            (p for p in identified if isinstance(p, SDFExecution)),
            None)
        if sdf_exec_sub:
            orderings = list(o for o in model.query_vertexes('orderings'))
            if orderings:
                res = SDFToOrders(
                    sdf_exec_sub=sdf_exec_sub,
                    orderings=orderings
                )
        # conditions for fixpoints and partial identification
        if res:
            res.compute_deduced_properties()
            return (True, res)
        elif sdf_exec_sub and not res:
            return (True, None)
        else:
            return (False, None)

    def compute_deduced_properties(self):
        sub = self.sdf_exec_sub
        cloned_firings = np.array([
            sub.sdf_repetition_vector.transpose()
            for i in range(1, len(sub.sdf_channels)+1)
        ])
        self.max_tokens = np.amax(
            cloned_firings * np.absolute(sub.sdf_topology)
        )

    def get_mzn_data(self):
        data = dict()
        sub = self.sdf_exec_sub
        data['sdf_actors'] = range(1, len(sub.sdf_actors)+1)
        data['sdf_channels'] = range(1, len(sub.sdf_channels)+1)
        data['max_steps'] = len(sub.sdf_pass)
        data['max_tokens'] = self.max_tokens
        data['activations'] = sub.sdf_repetition_vector[:, 0].tolist()
        data['static_orders'] = range(1, len(self.orderings)+1)
        return data

    def get_mzn_model_name(self):
        return 'sdf_order_linear_dmodel.mzn'

    def rebuild_forsyde_model(self, results):
        return ForSyDeModel()


@dataclass
class SDFToMultiCore(DecisionModel, MinizincAble):

    # covered decision models
    sdf_orders_sub: SDFToOrders = SDFToOrders()

    # partially identified
    cores: List[Vertex] = field(default_factory=list)
    busses: List[Vertex] = field(default_factory=list)
    connections: List[Edge] = field(default_factory=list)

    # deduced properties
    vertex_expansions: Dict[Vertex, List[Vertex]] = field(
        default_factory=dict
    )
    edge_expansions: Dict[Edge, List[Edge]] = field(
        default_factory=dict
    )
    cores_enum: Dict[Vertex, int] = field(
        default_factory=dict
    )
    comm_enum: Dict[Vertex, int] = field(
        default_factory=dict
    )
    expanded_cores_enum: Dict[Vertex, int] = field(
        default_factory=dict
    )
    expanded_comm_enum: Dict[Vertex, int] = field(
        default_factory=dict
    )



    @classmethod
    def identify(cls, model, identified):
        res = None
        sdf_orders_sub = next(
            (p for p in identified if isinstance(p, SDFToOrders)),
            None)
        if sdf_orders_sub:
            cores = list(model.query_vertexes('tdma_mpsoc_procs'))
            busses = list(model.query_vertexes('tdma_mpsoc_bus'))
            # this strange code access the in memory vertexes
            # representation by going through the labels (ids)
            # first, hence the get_vertex function.
            connections = set()
            for core in cores:
                for (v, adjdict) in model.adj[core].items():
                    for (n, e) in adjdict.items():
                        eobj = e['object']
                        if v in busses and eobj not in connections:
                            connections.append(eobj)
            for bus in busses:
                for (v, adjdict) in model.adj[bus].items():
                    for (n, e) in adjdict.items():
                        eobj = e['object']
                        if v in cores and eobj not in connections:
                            connections.append(eobj)
            if len(cores) + len(busses) >= len(sdf_orders_sub.orderings):
                res = SDFToMultiCore(
                    sdf_orders_sub=sdf_orders_sub,
                    cores=cores,
                    busses=busses,
                    connections=connections
                )
        # conditions for fixpoints and partial identification
        if res:
            res.compute_deduced_properties(model)
            return (True, res)
        elif not res and sdf_orders_sub:
            return (True, None)
        else:
            return (False, None)

    def get_mzn_model_name(self):
        return "sdf_mpsoc_linear_dmodel.mzn"

    def numbered_hw_units(self) -> Dict[str, int]:
        """
        """
        numbers = {}
        cur = 0
        for p in self.cores:
            numbers[p.identifier] = cur
            cur += 1
        for p in self.busses:
            numbers[p.identifier] = cur
            cur += 1
        return numbers

    def compute_deduced_properties(self):
        self.sdf_orders_sub.compute_deduced_properties()
        vertex_expansions = {p: [p] for p in self.cores}
        edge_expansions = {e : [] for e in self.connections}
        cores_enum = {p: i for (i, p) in enumerate(self.cores)}
        expanded_cores_enum = {p: i for (i, p) in enumerate(self.cores)}
        # expand all TDMAs to their slot elements
        comm_enum = dict()
        expanded_comm_enum = dict()
        units_enum_index = len(cores_enum)
        for (i, bus) in enumerate(self.busses):
            comm_enum[bus] = i + len(cores_enum)
            vertex_expansions[bus] = []
            for s in range(bus.properties['slots']):
                bus_slot = Vertex(
                    identifier=f'{bus.identifier}_slot_{s}'
                )
                expanded_comm_enum[bus_slot] = units_enum_index
                vertex_expansions[bus].append(bus_slot)
                units_enum_index += 1
        # now go through all the connections and
        # create copies of them as necessary to accomodate
        # the newly created processor and comm elements
        for (v, l) in vertex_expansions.items():
            for (o, ol) in vertex_expansions.items():
                for e in self.connections:
                    if e.source_vertex == v and e.target_vertex == o:
                        for v_new in l:
                            for o_new in ol:
                                expanded_e = Edge(
                                    source_vertex=v_new,
                                    target_vertex=o_new,
                                    edge_type=e.edge_type
                                )
                                edge_expansions[e].append(expanded_e)
        self.vertex_expansions = vertex_expansions
        self.edge_expansions = edge_expansions
        self.expanded_cores_enum = expanded_cores_enum
        self.expanded_comm_enum = expanded_comm_enum
        self.cores_enum = cores_enum
        self.comm_enum = comm_enum

    def get_mzn_data(self):
        data = self.sdf_orders_sub.get_mzn_data()
        expanded_units_enum = {
            **self.expanded_cores_enum,
            **self.expanded_comm_enum
        }
        data['procs'] = set(i+1 for i in self.expanded_cores_enum.values())
        data['comm_units'] = set(i+1 for i in self.expanded_comm_enum.values())
        data['units_neighs'] = [
            set(
                expanded_units_enum[el.target_vertex]+1
                for (e, el) in self.edge_expansions.items()
                for ex in el
                if el.source_vertex == u
            ).union(
                set(
                    expanded_units_enum[el.source_vertex]+1
                    for (e, el) in self.edge_expansions.items()
                    for ex in el
                    if el.target_vertex == u
                ))
            for (u, uidx) in expanded_units_enum.items()
        ]
        # since the minizinc model requires wcet and wcct,
        # we fake it with almost unitary assumption
        data['wcet'] = (data['max_tokens'] * np.ones((
            len(self['sdf_actors']),
            len(self.cores)
        ), dtype=int)).tolist()
        data['wcct'] = (np.ones((
            len(self['sdf_channels']),
            len(data['procs']) + len(data['comm_units']),
            len(data['procs']) + len(data['comm_units'])
        ), dtype=int)).tolist()
        # since the minizinc model requires objective weights,
        # we just disconsder them
        data['objective_weights'] = [0, 0]
        return data

    def rebuild_forsyde_model(self, results):
        sdf_exec_sub = self.sdf_orders_sub.sdf_exec_sub
        new_model = self.covered_model()
        for (aidx, a) in enumerate(results['mapped_actors']):
            actor = sdf_exec_sub.sdf_actors[aidx]
            for (pidx, p) in enumerate(a):
                ordering = self.sdf_orders_sub.orderings[pidx]
                core = self.cores[pidx]
                for (t, v) in enumerate(p):
                    if 0 < v and v < 2:
                        # TODO: fix multiple addition of elements here
                        if not new_model.has_edge(core, ordering):
                            edge = Edge(
                                core,
                                ordering,
                                None,
                                None,
                                TypesFactory.build_type('Mapping')
                            )
                            new_model.add_edge(
                                core,
                                ordering,
                                object=edge
                            )
                        if not new_model.has_edge(ordering, actor):
                            ord_port = Port(
                                identifier=f'slot{t}',
                                port_type=TypesFactory.build_type('Process')
                            )
                            ordering.ports.add(ord_port)
                            edge = Edge(
                                ordering,
                                actor,
                                ord_port,
                                None,
                                TypesFactory.build_type('Scheduling')
                            )
                            new_model.add_edge(
                                ordering,
                                actor,
                                object=edge
                            )
                    elif v > 1:
                        raise ValueError("Solution with pass must be implemented")
        # for (cidx, c) in enumerate(results['send']):
        #     channel = self['sdf_channels'][cidx]
        #     for (pidx, p) in enumerate(c):
        #         sender = self.cores[pidx]
        #         for (ppidx, pp) in enumerate(p):
        #             reciever = self.cores[ppidx]
        #             for (t, v) in enumerate(pp):
        #                 pass
        return new_model


@dataclass
class SDFToMultiCoreCharacterized(DecisionModel, MinizincAble):

    # covered partial identifications
    sdf_mpsoc_sub: SDFToMultiCore = SDFToMultiCore()

    # elements that are partially identified
    wcet: np.ndarray = np.array((0, 0), dtype=int)
    wcct: np.ndarray = np.array((0, 0, 0), dtype=int)
    throughput_importance: int = 0
    latency_importance: int = 0
    send_overhead: np.ndarray = np.array((0, 0), dtype=int)
    read_overhead: np.ndarray = np.array((0, 0), dtype=int)

    @classmethod
    def identify(cls, model, identified):
        res = None
        sdf_mpsoc_sub = next(
            (p for p in identified if isinstance(p, SDFToMultiCore)),
            None)
        if sdf_mpsoc_sub:
            sdf_actors = sdf_mpsoc_sub.sdf_orders_sub.sdf_exec_sub.sdf_actors
            sdf_channels = sdf_mpsoc_sub.sdf_orders_sub.sdf_exec_sub.sdf_channels
            cores = sdf_mpsoc_sub.cores
            busses = sdf_mpsoc_sub.busses
            units = cores + busses
            connections = sdf_mpsoc_sub.connections
            wcet = None
            wcct = None
            if next(model.query_view('count_wcet'))['count'] == len(cores) * len(sdf_actors):
                wcet = np.zeros(
                    (
                        len(cores),
                        len(sdf_actors),
                    ),
                    dtype=int
                )
                for row in model.query_view('wcet'):
                    app_index = next(
                        idx for (idx, v) in enumerate(sdf_actors)
                        if v.identifier == row['app_id']
                    )
                    plat_index = next(
                        idx for (idx, v) in enumerate(cores)
                        if v.identifier == row['plat_id']
                    )
                    wcet[app_index, plat_index] = int(row['wcet_time'])
            if next(model.query_view('count_signal_wcct'))['count'] == 2 * len(sdf_channels) * len(connections):
                wcct = np.zeros((len(sdf_channels), len(units), len(units)), dtype=int)
                for row in model.query_view('signal_wcct'):
                    sender_index = next(
                        idx for (idx, v) in enumerate(units)
                        if v.identifier == row['sender_id']
                    )
                    reciever_index = next(
                        idx for (idx, v) in enumerate(units)
                        if v.identifier == row['reciever_id']
                    )
                    signal_index = next(
                        idx for (idx, v) in enumerate(sdf_channels)
                        if v.identifier == row['signal_id']
                    )
                    wcct[signal_index, sender_index, reciever_index] = int(row['wcct_time'])
            # although there should be only one Th vertex
            # per application, we apply maximun just in case
            # someone forgot to make sure there is only one annotation
            # per application
            throughput_importance = 0
            throughput_targets = list(model.query_vertexes('min_throughput_targets'))
            if all(v in throughput_targets for v in sdf_mpsoc_sub['sdf_actors']):
                throughput_importance = max(
                    (int(v.properties['apriori_importance'])
                     for v in model.query_vertexes('min_throughput')),
                    default=0
                )
            if wcet is not None and wcct is not None:
                res = cls(
                    sdf_mpsoc_sub=sdf_mpsoc_sub,
                    wcet=wcet,
                    wcct=wcct,
                    throughput_importance=throughput_importance,
                    latency_importance=0
                )
        if res:
            return (True, res)
        elif sdf_mpsoc_sub and not res:
            return (True, None)
        else:
            return (False, None)

    def get_mzn_model_name(self):
        return "sdf_mpsoc_linear_dmodel.mzn"

    def get_mzn_data(self):
        data = self.sdf_mpsoc_sub.get_data_data()
        # use the non faked part of the covered problem
        # to save some code
        wcet_expanded = np.zeros(
            (
                len(self['sdf_actors']),
                sum(len(el) for el in cores_expansions.values())
            ),
            dtype=int
        )
        for (aidx, a) in enumerate(self['sdf_actors']):
            for (eidx, e) in enumerate(self['cores']):
                for ex in expansions[e.identifier]:
                    exidx = units_enum[ex]
                    wcet_expanded[aidx, exidx] = self.wcet[aidx, eidx]
        wcct_expanded = np.zeros(
            (
                len(self['sdf_channels']),
                sum(len(el) for el in expansions.values()),
                sum(len(el) for el in expansions.values())
            ),
            dtype=int
        )
        for (cidx, c) in enumerate(self['sdf_channels']):
            for (e, eidx) in pre_units_enum.items():
                for (e2, e2idx) in pre_units_enum.items():
                    for ex in expansions[e]:
                        exidx = units_enum[ex]
                        for ex2 in expansions[e2]:
                            ex2idx = units_enum[ex2]
                            wcct_expanded[cidx, exidx, ex2idx] = self.wcct[cidx, eidx, e2idx]
        data['wcet'] = wcet_expanded.tolist()
        data['wcct'] = wcct_expanded.tolist()
        # data['send_overhead'] = (np.zeros((
        #     len(self['sdf_channels']),
        #     len(self['cores'])
        # ), dtype=int)).tolist()
        # data['read_overhead'] = (np.zeros((
        #     len(self['sdf_channels']),
        #     len(self['cores'])
        # ), dtype=int)).tolist()
        data['objective_weights'] = [
            self.throughput_importance,
            self.latency_importance
        ]
        return data

    def rebuild_forsyde_model(self, results):
        print(results['mapped_actors'])
        print(results['objective'])
        print(results['time'])
        new_model = self.covered_model()
        for (aidx, a) in enumerate(results['mapped_actors']):
            actor = self['sdf_actors'][aidx]
            for (pidx, p) in enumerate(a):
                ordering = self['orderings'][pidx]
                core = self['cores'][pidx]
                for (t, v) in enumerate(p):
                    if 0 < v and v < 2:
                        # TODO: fix multiple addition of elements here
                        if not new_model.has_edge(core, ordering):
                            exec_port = Port(
                                'execution',
                                TypesFactory.build_type('AbstractGrouping'),
                            )
                            core.ports.add(exec_port)
                            new_model.add_edge(
                                core,
                                ordering,
                                object=Edge(
                                    core,
                                    ordering,
                                    exec_port,
                                    None,
                                    TypesFactory.build_type('Mapping')
                                )
                            )
                        if not new_model.has_edge(ordering, actor):
                            ord_port = Port(
                                identifier = f'slot[{t}]',
                                port_type = TypesFactory.build_type('Process')
                            )
                            ordering.ports.add(ord_port)
                            new_model.add_edge(
                                ordering,
                                actor,
                                object=Edge(
                                    ordering,
                                    actor,
                                    ord_port,
                                    None,
                                    TypesFactory.build_type('Scheduling')
                                )
                            )
                    elif v > 1:
                        raise ValueError("Solution with pass must be implemented")
        (expansions, enum_items) = self.sdf_mpsoc_sub.expanded_hw_units()
        items_enums = {i: p for (i, p) in enum_items.items()}
        for (cidx, c) in enumerate(results['send']):
            channel = self['sdf_channels'][cidx]
            for (pidx, p) in enumerate(c):
                # sender = self['cores'][pidx]
                for (ppidx, pp) in enumerate(p):
                    # reciever = self['cores'][ppidx]
                    for (t, v) in enumerate(pp):
                        pass
        return new_model


@dataclass
class SDFToMultiCoreCharacterizedJobs(DecisionModel, MinizincAble):

    sdf_mpsoc_char_sub: SDFToMultiCoreCharacterized
    jobs: List[str] = field(default_factory=list)

    @classmethod
    def identify(cls, model, identified):
        res = None
        sdf_mpsoc_char_sub = next(
            (p for p in identified if isinstance(p, SDFToMultiCoreCharacterized)),
            None)
        if sdf_mpsoc_char_sub:
            jobs = {
                f"{a.identifier}_{i}": (a.identifier, aidx)
                for (aidx, a) in enumerate(sdf_mpsoc_char_sub['sdf_actors'])
                    for i in sdf_mpsoc_char_sub['sdf_repetition_vector'][aidx]
            }
            res = cls(
                sdf_mpsoc_char_sub=sdf_mpsoc_char_sub,
                jobs=jobs
            )
        if res:
            return (True, res)
        else:
            return (False, None)

    def get_mzn_model_name(self):
        return "sdf_job_scheduling.mzn"

    def populate_mzn_model(self, mzn):
        # use the non faked part of the covered problem
        # to save some code
        pre_units_enum = self.sdf_mpsoc_char_sub.numbered_hw_units()
        (expansions, units_enum, cores_enum, comm_enum) = self.sdf_mpsoc_char_sub.expanded_hw_units()
        cores_enum = {}
        cores_expansions = {}
        for (e, el) in expansions.items():
            if any(p.identifier == e for p in self['cores']):
                cores_expansions[e] = el
                for ex in el:
                    cores_enum[ex] = units_enum[ex]
        comm_enum = {}
        comm_expansions = {}
        for (e, el) in expansions.items():
            if any(p.identifier == e for p in self['busses']):
                comm_expansions[e] = el
                for ex in el:
                    comm_enum[ex] = units_enum[ex]
        wcet_expanded = np.zeros(
            (
                len(self['sdf_actors']),
                sum(len(el) for el in cores_expansions.values())
            ),
            dtype=int
        )
        for (aidx, a) in enumerate(self['sdf_actors']):
            for (eidx, e) in enumerate(self['cores']):
                for ex in expansions[e.identifier]:
                    exidx = units_enum[ex]
                    wcet_expanded[aidx, exidx] = self['wcet'][aidx, eidx]
        wcct_expanded = np.zeros(
            (
                len(self['sdf_channels']),
                sum(len(el) for el in expansions.values()),
                sum(len(el) for el in expansions.values())
            ),
            dtype=int
        )
        for (cidx, c) in enumerate(self['sdf_channels']):
            for (e, eidx) in pre_units_enum.items():
                for (e2, e2idx) in pre_units_enum.items():
                    for ex in expansions[e]:
                        exidx = units_enum[ex]
                        for ex2 in expansions[e2]:
                            ex2idx = units_enum[ex2]
                            wcct_expanded[cidx, exidx, ex2idx] = self['wcct'][cidx, eidx, e2idx]
        cloned_firings = np.array([
            self['sdf_repetition_vector'].transpose()
            for i in range(1, len(self['sdf_channels'])+1)
        ])
        max_tokens = np.amax(
            cloned_firings * np.absolute(self['sdf_topology'])
        )
        mzn['max_tokens'] = int(max_tokens)
        mzn['sdf_actors'] = range(1, len(self['sdf_actors'])+1)
        mzn['sdf_channels'] = range(1, len(self['sdf_channels'])+1)
        mzn['procs'] = set(i+1 for i in cores_enum.values())
        mzn['comm_units'] = set(i+1 for i in comm_enum.values())
        mzn['jobs'] = range(1, len(self.jobs)+1)
        # mzn['activations'] = self['sdf_repetition_vector'][:, 0].tolist()
        mzn['jobs_actors'] = [
            aidx+1
            for (j, (label, aidx)) in self.jobs.items()
        ]
        # TODO: must fix the delays in the model
        mzn['initial_tokens'] = [0 for a in self['sdf_actors']]
        mzn['sdf_topology'] = self['sdf_topology'].tolist()
        mzn['wcet'] = wcet_expanded.tolist()
        mzn['wcct'] = wcct_expanded.tolist()
        mzn['units_neighs'] = [
            set(
                units_enum[ex]
                for e in self['connections']
                for ex in expansions[e.target_vertex.identifier]
                if e.source_vertex.identifier == u
            ).union(
            set(
                units_enum[ex]
                for e in self['connections']
                for ex in expansions[e.source_vertex.identifier]
                if e.target_vertex.identifier == u
            ))
            for (u, uidx) in units_enum.items()
        ]
        # mzn['send_overhead'] = (np.zeros((
        #     len(self['sdf_channels']),
        #     len(self['cores'])
        # ), dtype=int)).tolist()
        # mzn['read_overhead'] = (np.zeros((
        #     len(self['sdf_channels']),
        #     len(self['cores'])
        # ), dtype=int)).tolist()
        mzn['objective_weights'] = [
            self['throughput_importance'],
            self['latency_importance']
        ]
        return mzn

    def rebuild_forsyde_model(self, results):
        new_model = self.covered_model()
        print(results)
        return new_model


def _get_standard_problems() -> Set[Type[DecisionModel]]:
    return set(c for c in DecisionModel.__subclasses__())


def identify_decision_models(
    model: ForSyDeModel,
    problems: Set[Type[DecisionModel]] = _get_standard_problems()
) -> List[DecisionModel]:
    '''
    This function runs the Design Space Identification scheme,
    as presented in paper [DSI-DATE'2021], so that problems can
    be automatically solved from the given input model.

    If the argument **problems** is not passed,
    the API uses all subclasses found during runtime that implement
    the interfaces DecisionModel and Explorer.
    '''
    max_iterations = len(model) * len(problems)
    candidates = [p for p in problems]
    identified: List[DecisionModel] = []
    iterations = 0
    while len(candidates) > 0 and iterations < max_iterations:
        trials = ((c, c.identify(model, identified)) for c in candidates)
        for (c, (fixed, subprob)) in trials:
            # join with the identified
            if subprob:
                identified.append(subprob)
            # take away candidates at fixpoint
            if fixed:
                candidates.remove(c)
        iterations += 1
    return identified


def identify_decision_models_parallel(
    model: ForSyDeModel,
    problems: Set[Type[DecisionModel]] = set(),
    concurrent_idents: int = os.cpu_count() or 1
) -> List[DecisionModel]:
    '''
    This function runs the Design Space Identification scheme,
    as presented in paper [DSI-DATE'2021], so that problems can
    be automatically solved from the given input model. It also
    uses parallelism to run as many identifications as possible
    simultaneously.

    If the argument **problems** is not passed,
    the API uses all subclasses found during runtime that implement
    the interfaces DecisionModel and Explorer.
    '''
    max_iterations = len(model) * len(problems)
    candidates = [p for p in problems]
    identified: List[DecisionModel] = []
    iterations = 0
    with concurrent.futures.ProcessPoolExecutor(
            max_workers=concurrent_idents) as executor:
        while len(candidates) > 0 and iterations < max_iterations:
            # generate all trials and keep track of which subproblem
            # made the trial
            futures = {
                c: executor.submit(c.identify, model, identified)
                for c in candidates
            }
            concurrent.futures.wait(futures.values())
            for c in futures:
                (fixed, subprob) = futures[c].result()
                # join with the identified
                if subprob:
                    identified.append(subprob)
                # take away candidates at fixpoint
                if fixed:
                    candidates.remove(c)
            iterations += 1
        return identified


def choose_decision_models(
    models: List[DecisionModel],
    criteria: ChoiceCriteria = ChoiceCriteria.DOMINANCE
) -> List[DecisionModel]:
    if criteria & ChoiceCriteria.DOMINANCE:
        non_dominated = [m for m in models]
        for m in models:
            for other in models:
                if m in non_dominated and m != other and other.dominates(m):
                    non_dominated.remove(m)
        return non_dominated
    else:
        return models


async def identify_decision_models_async(
    model: ForSyDeModel,
    problems: Set[Type[DecisionModel]] = set()
) -> List[DecisionModel]:
    '''
    AsyncIO version of the same function. Wraps the non-async version.
    '''
    return identify_decision_models(model, problems)


async def identify_decision_models_parallel_async(
    model: ForSyDeModel,
    problems: Set[Type[DecisionModel]] = set(),
    concurrent_idents: int = os.cpu_count() or 1
) -> List[DecisionModel]:
    '''
    AsyncIO version of the same function. Wraps the non-async version.
    '''
    return identify_decision_models_parallel(
        model,
        problems,
        concurrent_idents
    )
