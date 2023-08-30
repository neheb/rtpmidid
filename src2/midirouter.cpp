/**
 * Real Time Protocol Music Instrument Digital Interface Daemon
 * Copyright (C) 2019-2023 David Moreno Montero <dmoreno@coralbits.com>
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#include "midirouter.hpp"
#include "mididata.hpp"
#include "midipeer.hpp"
#include "rtpmidid/logger.hpp"

using namespace rtpmididns;

midirouter_t::midirouter_t() : max_id(1) {}

uint32_t midirouter_t::add_peer(std::shared_ptr<midipeer_t> ptr) {
  auto peer_id = max_id++;
  ptr->peer_id = peer_id;
  ptr->router = this;

  peers[peer_id] = peerconnection_t{
      peer_id,
      ptr,
      {},
  };
  INFO("Added peer {}", peer_id);

  return peer_id;
}

void midirouter_t::remove_peer(peer_id_t peer_id) {
  peers.erase(peer_id);
  INFO("Removed peer {}", peer_id);
}

void midirouter_t::send_midi(uint32_t from, const mididata_t &data) {
  auto peer = peers.find(from);
  if (peer == peers.end()) {
    WARNING("Sending from an uknown peer {}!", from);
    return;
  }

  // DEBUG("Send data to {} peers", peer->second.send_to.size());
  for (auto to : peer->second.send_to) {
    // DEBUG("Send data {} to {}", from, to);
    send_midi(from, to, data);
  }
}

void midirouter_t::send_midi(peer_id_t from, peer_id_t to,
                             const mididata_t &data) {
  auto send_to_peer = peers.find(to);
  if (send_to_peer == peers.end()) {
    WARNING("Sending to uknown peer {} -> {}", from, to);
    return; // Maybe better delete
  }
  auto send_from_peer = peers.find(from);
  if (send_from_peer == peers.end()) {
    WARNING("Sending to uknown peer {} -> {}", from, to);
    return; // Maybe better delete
  }

  send_from_peer->second.peer->packets_sent++;
  send_to_peer->second.peer->packets_recv++;

  send_to_peer->second.peer->send_midi(from, data);
}

void midirouter_t::connect(peer_id_t from, peer_id_t to) {
  auto peer = peers.find(from);
  if (peer == peers.end()) {
    WARNING("Sending from an uknown peer {}!", from);
    return;
  }
  auto peerto = peers.find(to);
  if (peerto == peers.end()) {
    WARNING("Sending to an uknown peer {}!", from);
    return;
  }

  peer->second.send_to.push_back(to);
}