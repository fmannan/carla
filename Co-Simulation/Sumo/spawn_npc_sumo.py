#!/usr/bin/env python

# Copyright (c) 2019 Computer Vision Center (CVC) at the Universitat Autonoma de
# Barcelona (UAB).
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.
"""Spawn Sumo NPCs vehicles into the simulation"""

# ==================================================================================================
# -- imports ---------------------------------------------------------------------------------------
# ==================================================================================================

import argparse
import datetime
import json
import logging
import time
import random
import re
import shutil

import lxml.etree as ET  # pylint: disable=wrong-import-position

# ==================================================================================================
# -- find carla module -----------------------------------------------------------------------------
# ==================================================================================================

import glob
import os
import sys

try:
    sys.path.append(
        glob.glob('../../PythonAPI/carla/dist/carla-*%d.%d-%s.egg' %
                  (sys.version_info.major, sys.version_info.minor,
                   'win-amd64' if os.name == 'nt' else 'linux-x86_64'))[0])
except IndexError:
    pass

# ==================================================================================================
# -- find traci module -----------------------------------------------------------------------------
# ==================================================================================================

if 'SUMO_HOME' in os.environ:
    sys.path.append(os.path.join(os.environ['SUMO_HOME'], 'tools'))
else:
    sys.exit("please declare environment variable 'SUMO_HOME'")

# ==================================================================================================
# -- carla -----------------------------------------------------------------------------------------
# ==================================================================================================

import carla  # pylint: disable=wrong-import-position

import sumolib  # pylint: disable=wrong-import-position
import traci  # pylint: disable=wrong-import-position

from sumo_integration.carla_simulation import CarlaSimulation  # pylint: disable=wrong-import-position
from sumo_integration.sumo_simulation import SumoSimulation  # pylint: disable=wrong-import-position

from run_synchronization import SimulationSynchronization  # pylint: disable=wrong-import-position

# ==================================================================================================
# -- main ------------------------------------------------------------------------------------------
# ==================================================================================================


def write_sumocfg_xml(town_name, tmp_path, net_file, vtypes_file, viewsettings_file):
    """
    Writes sumo configuration xml file.
    """

    root = ET.Element('configuration')

    input_tag = ET.SubElement(root, 'input')
    ET.SubElement(input_tag, 'net-file', {'value': net_file})
    ET.SubElement(input_tag, 'route-files', {'value': vtypes_file})

    gui_tag = ET.SubElement(root, 'gui_only')
    ET.SubElement(gui_tag, 'gui-settings-file', {'value': viewsettings_file})

    tree = ET.ElementTree(root)
    filename = os.path.join(tmp_path, town_name + '.sumocfg')
    tree.write(filename, pretty_print=True, encoding='UTF-8', xml_declaration=True)

    return filename


def get_sumo_files(town_name, tmp_path):
    """
    Returns sumo configuration file and sumo net.
    """
    base_path = os.path.dirname(os.path.realpath(__file__))
    net_file = os.path.join(base_path, 'examples', 'net', town_name + '.net.xml')
    vtypes_file = os.path.join(base_path, 'examples', 'carlavtypes.rou.xml')
    viewsettings_file = os.path.join(base_path, 'examples', 'viewsettings.xml')

    cfg_file = write_sumocfg_xml(town_name, tmp_path, net_file, vtypes_file, viewsettings_file)
    return cfg_file, net_file


def main(args):

    # Creating temporal folder to save configuration file.
    tmp_path = 'spawn_npc_sumo-{date:%Y-%m-%d_%H-%M-%S-%f}'.format(date=datetime.datetime.now())
    if not os.path.exists(tmp_path):
        os.mkdir(tmp_path)

    try:
        # ----------------
        # carla simulation
        # ----------------
        carla_simulation = CarlaSimulation(args.host, args.port, args.step_length)

        world = carla_simulation.client.get_world()
        current_map = world.get_map().name
        if current_map not in ('Town01', 'Town04', 'Town05'):
            raise RuntimeError('This script does not support {} yet.'.format(current_map))

        # ---------------
        # sumo simulation
        # ---------------
        cfg_file, net_file = get_sumo_files(current_map, tmp_path)

        sumo_net = sumolib.net.readNet(net_file)
        sumo_simulation = SumoSimulation(None, None, args.step_length, cfg_file, args.sumo_gui)

        # ---------------
        # synchronization
        # ---------------
        synchronization = SimulationSynchronization(sumo_simulation, carla_simulation,
                                                    args.tls_manager, args.sync_vehicle_color,
                                                    args.sync_vehicle_lights)

        # ----------
        # Blueprints
        # ----------
        with open('data/vtypes.json') as f:
            vtypes = json.load(f)['carla_blueprints']

        blueprints = vtypes.keys()

        filterv = re.compile(args.filterv)
        blueprints = list(filter(filterv.search, blueprints))

        if args.safe:
            blueprints = [
                x for x in blueprints if vtypes[x]['vClass'] not in ('motorcycle', 'bicycle')
            ]
            blueprints = [x for x in blueprints if not x.endswith('isetta')]
            blueprints = [x for x in blueprints if not x.endswith('carlacola')]
            blueprints = [x for x in blueprints if not x.endswith('cybertruck')]
            blueprints = [x for x in blueprints if not x.endswith('t2')]

        if not blueprints:
            raise RuntimeError('No blueprints available due to user restrictions.')

        if args.number_of_walkers > 0:
            logging.warning('Pedestrians are not supported yet. No walkers will be spawned.')

        # --------------
        # Spawn vehicles
        # --------------
        # Spawns sumo NPC vehicles.
        sumo_edges = sumo_net.getEdges()

        for i in range(args.number_of_vehicles):
            edge = random.choice(sumo_edges)
            type_id = random.choice(blueprints)

            traci.route.add('route_{}'.format(i), [edge.getID()])
            result = traci.vehicle.add('sumo_{}'.format(i), 'route_{}'.format(i), typeID=type_id)

        while True:
            start = time.time()

            synchronization.tick()

            # Updates vehicle routes
            for vehicle_id in traci.vehicle.getIDList():
                route = traci.vehicle.getRoute(vehicle_id)
                index = traci.vehicle.getRouteIndex(vehicle_id)
                vclass = traci.vehicle.getVehicleClass(vehicle_id)

                if index == (len(route) - 1):
                    current_edge = sumo_net.getEdge(route[index])
                    next_edge = random.choice(list(current_edge.getAllowedOutgoing(vclass).keys()))

                    new_route = [current_edge.getID(), next_edge.getID()]
                    traci.vehicle.setRoute(vehicle_id, new_route)

            end = time.time()
            elapsed = end - start
            if elapsed < args.step_length:
                time.sleep(args.step_length - elapsed)

    except KeyboardInterrupt:
        logging.info('Cancelled by user.')

    finally:
        if os.path.exists(tmp_path):
            shutil.rmtree(tmp_path)

        logging.info('Destroying %d sumo vehicles.', traci.vehicle.getIDCount())
        synchronization.close()


if __name__ == '__main__':
    argparser = argparse.ArgumentParser(description=__doc__)
    argparser.add_argument('--host',
                           metavar='H',
                           default='127.0.0.1',
                           help='IP of the host server (default: 127.0.0.1)')
    argparser.add_argument('-p',
                           '--port',
                           metavar='P',
                           default=2000,
                           type=int,
                           help='TCP port to listen to (default: 2000)')
    argparser.add_argument('-n',
                           '--number-of-vehicles',
                           metavar='N',
                           default=10,
                           type=int,
                           help='number of vehicles (default: 10)')
    argparser.add_argument('-w',
                           '--number-of-walkers',
                           metavar='W',
                           default=0,
                           type=int,
                           help='number of walkers (default: 0)')
    argparser.add_argument('--safe',
                           action='store_true',
                           help='avoid spawning vehicles prone to accidents')
    argparser.add_argument('--filterv',
                           metavar='PATTERN',
                           default='vehicle.*',
                           help='vehicles filter (default: "vehicle.*")')
    argparser.add_argument('--filterw',
                           metavar='PATTERN',
                           default='walker.pedestrian.*',
                           help='pedestrians filter (default: "walker.pedestrian.*")')
    argparser.add_argument('--sumo-gui',
                           action='store_true',
                           help='run the gui version of sumo')
    argparser.add_argument('--step-length',
                           default=0.05,
                           type=float,
                           help='set fixed delta seconds (default: 0.05s)')
    argparser.add_argument('--sync-vehicle-lights',
                           action='store_true',
                           help='synchronize vehicle lights state (default: False)')
    argparser.add_argument('--sync-vehicle-color',
                           action='store_true',
                           help='synchronize vehicle color (default: False)')
    argparser.add_argument('--sync-vehicle-all',
                           action='store_true',
                           help='synchronize all vehicle properties (default: False)')
    argparser.add_argument('--tls-manager',
                           type=str,
                           choices=['none', 'sumo', 'carla'],
                           help="select traffic light manager (default: none)",
                           default='none')
    argparser.add_argument('--debug', action='store_true', help='enable debug messages')
    args = argparser.parse_args()

    if args.sync_vehicle_all is True:
        args.sync_vehicle_lights = True
        args.sync_vehicle_color = True

    if args.debug:
        logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.DEBUG)
    else:
        logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)

    main(args)