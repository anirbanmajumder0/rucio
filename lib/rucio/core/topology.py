# -*- coding: utf-8 -*-
# Copyright European Organization for Nuclear Research (CERN) since 2012
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import itertools
from typing import TYPE_CHECKING

from dogpile.cache.api import NoValue
from sqlalchemy import select, false

from rucio.common.utils import PriorityQueue
from rucio.common.cache import make_region_memcached
from rucio.common.config import config_get_int
from rucio.common.exception import NoDistance, RSEProtocolNotSupported
from rucio.core.rse import RseCollection
from rucio.db.sqla import models
from rucio.db.sqla.session import transactional_session
from rucio.rse import rsemanager as rsemgr

if TYPE_CHECKING:
    from typing import Any, Dict, List, Optional, Union
    from sqlalchemy.orm import Session

REGION = make_region_memcached(expiration_time=600)


class Topology:
    """
    Helper private class used to dynamically load and cache the rse information
    """
    def __init__(self, rse_collection: "RseCollection"):
        self.rse_collection = rse_collection

    @transactional_session
    def search_shortest_paths(
            self,
            source_rse_ids: "List[str]",
            dest_rse_id: "List[str]",
            operation_src: str,
            operation_dest: str,
            domain: str,
            multihop_rses: "List[str]",
            limit_dest_schemes: "Union[str, List[str]]",
            inbound_links_by_node: "Optional[Dict[str, Dict[str, str]]]" = None,
            session: "Optional[Session]" = None
    ) -> "Dict[str, List[Dict[str, Any]]]":
        """
        Find the shortest paths from multiple sources towards dest_rse_id.
        Does a Backwards Dijkstra's algorithm: start from destination and follow inbound links towards the sources.
        If multihop is disabled, stop after analysing direct connections to dest_rse. Otherwise, stops when all
        sources where found or the graph was traversed in integrality.

        The inbound links retrieved from the database can be accumulated into the inbound_links_by_node, passed
        from the calling context. To be able to reuse them.
        """
        HOP_PENALTY = config_get_int('transfers', 'hop_penalty', default=10, session=session)  # Penalty to be applied to each further hop

        self.rse_collection.ensure_loaded(itertools.chain(source_rse_ids, [dest_rse_id], multihop_rses),
                                          load_attributes=True, load_info=True, session=session)
        if multihop_rses:
            # Filter out island source RSEs
            sources_to_find = {rse_id for rse_id in source_rse_ids if _load_outgoing_distances_node(rse_id=rse_id, session=session)}
        else:
            sources_to_find = set(source_rse_ids)

        next_hop = {dest_rse_id: {'cumulated_distance': 0}}
        priority_q = PriorityQueue()

        remaining_sources = copy.copy(sources_to_find)
        priority_q[dest_rse_id] = 0
        while priority_q:
            current_node = priority_q.pop()

            if current_node in remaining_sources:
                remaining_sources.remove(current_node)
            if not remaining_sources:
                # We found the shortest paths to all desired sources
                break

            current_distance = next_hop[current_node]['cumulated_distance']
            inbound_links = _load_inbound_distances_node(rse_id=current_node)
            if inbound_links_by_node is not None:
                inbound_links_by_node[current_node] = inbound_links
            for adjacent_node, link_distance in sorted(inbound_links.items(),
                                                       key=lambda item: 0 if item[0] in sources_to_find else 1):
                if link_distance is None:
                    continue

                if adjacent_node not in remaining_sources and adjacent_node not in multihop_rses:
                    continue

                try:
                    hop_penalty = int(self.rse_collection[adjacent_node].attributes.get('hop_penalty', HOP_PENALTY))
                except ValueError:
                    hop_penalty = HOP_PENALTY
                new_adjacent_distance = current_distance + link_distance + hop_penalty
                if next_hop.get(adjacent_node, {}).get('cumulated_distance', 9999) <= new_adjacent_distance:
                    continue

                try:
                    matching_scheme = rsemgr.find_matching_scheme(
                        rse_settings_src=self.rse_collection[adjacent_node].info,
                        rse_settings_dest=self.rse_collection[current_node].info,
                        operation_src=operation_src,
                        operation_dest=operation_dest,
                        domain=domain,
                        scheme=limit_dest_schemes if adjacent_node == dest_rse_id and limit_dest_schemes else None
                    )
                    next_hop[adjacent_node] = {
                        'source_rse_id': adjacent_node,
                        'dest_rse_id': current_node,
                        'source_scheme': matching_scheme[1],
                        'dest_scheme': matching_scheme[0],
                        'source_scheme_priority': matching_scheme[3],
                        'dest_scheme_priority': matching_scheme[2],
                        'hop_distance': link_distance,
                        'cumulated_distance': new_adjacent_distance,
                    }
                    priority_q[adjacent_node] = new_adjacent_distance
                except RSEProtocolNotSupported:
                    if next_hop.get(adjacent_node) is None:
                        next_hop[adjacent_node] = {}

            if not multihop_rses:
                # Stop after the first iteration, which finds direct connections to destination
                break

        paths = {}
        for rse_id in source_rse_ids:
            hop = next_hop.get(rse_id)
            if hop is None:
                continue

            path = []
            while hop.get('dest_rse_id'):
                path.append(hop)
                hop = next_hop.get(hop['dest_rse_id'])
            paths[rse_id] = path
        return paths


@transactional_session
def get_hops(source_rse_id, dest_rse_id, multihop_rses=None, limit_dest_schemes=None, session=None):
    """
    Get a list of hops needed to transfer date from source_rse_id to dest_rse_id.
    Ideally, the list will only include one item (dest_rse_id) since no hops are needed.
    :param source_rse_id:       Source RSE id of the transfer.
    :param dest_rse_id:         Dest RSE id of the transfer.
    :param multihop_rses:       List of RSE ids that can be used for multihop. If empty, multihop is disabled.
    :param limit_dest_schemes:  List of destination schemes the matching scheme algorithm should be limited to for a single hop.
    :returns:                   List of hops in the format [{'source_rse_id': source_rse_id, 'source_scheme': 'srm', 'source_scheme_priority': N, 'dest_rse_id': dest_rse_id, 'dest_scheme': 'srm', 'dest_scheme_priority': N}]
    :raises:                    NoDistance
    """
    if not limit_dest_schemes:
        limit_dest_schemes = []

    if not multihop_rses:
        multihop_rses = []

    topology = Topology(RseCollection())
    shortest_paths = topology.search_shortest_paths(source_rse_ids=[source_rse_id], dest_rse_id=dest_rse_id,
                                                    operation_src='third_party_copy_read', operation_dest='third_party_copy_write',
                                                    domain='wan', multihop_rses=multihop_rses,
                                                    limit_dest_schemes=limit_dest_schemes, session=session)

    result = REGION.get('get_hops_dist_%s_%s_%s' % (str(source_rse_id), str(dest_rse_id), ''.join(sorted(limit_dest_schemes))))
    if not isinstance(result, NoValue):
        return result

    path = shortest_paths.get(source_rse_id)
    if path is None:
        raise NoDistance()

    if not path:
        raise RSEProtocolNotSupported()

    REGION.set('get_hops_dist_%s_%s_%s' % (str(source_rse_id), str(dest_rse_id), ''.join(sorted(limit_dest_schemes))), path)
    return path


@transactional_session
def _load_outgoing_distances_node(rse_id, session=None):
    """
    Loads the outgoing edges of the distance graph for one node.
    :param rse_id:    RSE id to load the edges for.
    :param session:   The DB Session to use.
    :returns:         Dictionary based graph object.
    """

    result = REGION.get('outgoing_edges_%s' % str(rse_id))
    if isinstance(result, NoValue):
        outgoing_edges = {}
        stmt = select(
            models.Distance
        ).join(
            models.RSE,
            models.RSE.id == models.Distance.dest_rse_id
        ).where(
            models.Distance.src_rse_id == rse_id,
            models.RSE.deleted == false()
        )
        for distance in session.execute(stmt).scalars():
            if distance.ranking is None:
                continue
            ranking = distance.ranking if distance.ranking >= 0 else 0
            outgoing_edges[distance.dest_rse_id] = ranking
        REGION.set('outgoing_edges_%s' % str(rse_id), outgoing_edges)
        result = outgoing_edges
    return result


@transactional_session
def _load_inbound_distances_node(rse_id, session=None):
    """
    Loads the inbound edges of the distance graph for one node.
    :param rse_id:    RSE id to load the edges for.
    :param session:   The DB Session to use.
    :returns:         Dictionary based graph object.
    """

    result = REGION.get('inbound_edges_%s' % str(rse_id))
    if isinstance(result, NoValue):
        inbound_edges = {}
        stmt = select(
            models.Distance
        ).join(
            models.RSE,
            models.RSE.id == models.Distance.src_rse_id
        ).where(
            models.Distance.dest_rse_id == rse_id,
            models.RSE.deleted == false()
        )
        for distance in session.execute(stmt).scalars():
            if distance.ranking is None:
                continue
            ranking = distance.ranking if distance.ranking >= 0 else 0
            inbound_edges[distance.src_rse_id] = ranking
        REGION.set('inbound_edges_%s' % str(rse_id), inbound_edges)
        result = inbound_edges
    return result
