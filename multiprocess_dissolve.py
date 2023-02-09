from multiprocessing import Pool
from collections.abc import Iterable

import json
import os
import queue
import geopandas as gpd
import pandas as pd
import numpy as np

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NpEncoder, self).default(obj)

def make_tree_up(df: pd.DataFrame, order: int = 0, stream_id_col: str = "COMID", next_down_id_col: str = "NextDownID", order_col: str = "order_") -> dict:
    """
    Makes a dictionary depicting a tree where each segment id as a key has a tuple containing the ids of its parent segments, or the ones that
    have it as the next down id. Either does this for every id in the tree, or only includes ids of a given stream order
    and their parent of the same stream order, if they have one. This function attempts to use pandas vectorization and
    indexing to improve efficiency on make_tree, but may miss some edge cases, further testing is underway.
    Args:
        df: dataframe to parse the tree from. Must contain:
            - a column with the segment/catchment ID ("HydroID")
            - a column with the IDs for the next down segment ("NextDownID")
            - an order column
        order: number of stream order to limit it to. If zero, will make tree for all segments,
               otherwise only contains ids that match the given stream order.
        stream_id_col: the name of the column that contains the unique ids for the streams
        next_down_id_col: the name of the column that contains the unique id of the next down stream for each row, the
                          one that the stream for that row feeds into.
        order_col: name of the column that contains the stream order
    Returns: dictionary where for each key, a tuple of all values that have that key as their next down id is assigned.
             if order==0, values will be either length 0 or 2, otherwise will be 0 or one as only a maximum of
             one parent will be of the given order.
    """
    if order == 0:
        out = df[[stream_id_col, next_down_id_col]].set_index(next_down_id_col)
        out.drop(-1, inplace=True)
        tree = {}
        for hydroid in df[stream_id_col]:
            if hydroid in out.index:
                rows = out.loc[hydroid][stream_id_col]
                if not (isinstance(rows, np.floating) or isinstance(rows, np.generic)):
                    tree[hydroid] = tuple(rows.tolist())
                else:
                    tree[hydroid] = (rows,)
            else:
                tree[hydroid] = ()
        return tree
    out = df[df[order_col] == order][[stream_id_col, next_down_id_col, order_col]].set_index(next_down_id_col)
    tree = {hydroid: ((int(out.loc[hydroid][stream_id_col]),) if hydroid in out.index else ()) for hydroid in
            df[df[order_col] == order][stream_id_col]}
    return tree


def make_tree_down(df: pd.DataFrame, order: int = 0, stream_id_col: str = "COMID", next_down_id_col: str = "NextDownID", order_col: str = "order_") -> dict:
    """
    Performs the simpler task of pairing segment ids as keys with their next down ids as values.
    Args:
        df: dataframe to parse the tree from. Must contain:
            - a column with the segment/catchment ID ("HydroID")
            - a column with the IDs for the next down segment ("NextDownID")
            - an order column
        order: number of stream order to limit it to. If zero, will make tree for all segments,
               otherwise only contains ids that match the given stream order.
        stream_id_col: the name of the column that contains the unique ids for the streams
        next_down_id_col: the name of the column that contains the unique id of the next down stream for each row, the
                          one that the stream for that row feeds into.
        order_col: name of the column that contains the stream order
    Returns: dictionary where for each key its next down id from the dataframe is given as a value.
    """
    if order == 0:
        tree = dict(zip(df[stream_id_col], df[next_down_id_col]))
        return tree
    out = df[[stream_id_col, next_down_id_col, order_col]][df[order_col] == order]
    out_2 = out[out[next_down_id_col].isin(out[stream_id_col])]
    out.loc[~out[stream_id_col].isin(out_2[stream_id_col]), next_down_id_col] = -1
    # tree = dict(zip(out.loc[out['NextDownID'] != -1, 'HydroID'], out.loc[out['NextDownID'] != -1, 'NextDownID']))
    tree = dict(zip(out[stream_id_col], out[next_down_id_col]))
    return tree


def trace_tree(tree: dict, search_id: int, cuttoff_n: int = 200) -> list:
    """
    Universal function that traces a tree produced by make_tree_up or make_tree_down from the search id all the way to
    the end of the segment. If the given tree was produced for a given order, it will produce a list with all down or
    upstream segments that share that order. If the tree was produced including all segments, it will get the whole
    upstream network or the whole path to the ocean.
    Args:
        tree: Tree where each key has a tuple containing the ids of each of its parent segments, or an integer of
              a single child, as its value, i.e.:
              {2: (3, 5), 3: (), 4: (): 5: (6, 7), 6: (), 7: ()} for an upstream tree
                or
              {2: -1, 3: 2, 5: 2, 4: -1, 6: 5, 7: 5} for a downstream tree
        search_id: id to search from.
    Returns: list containing all ids that will be upstream of the search_id.
    """
    q = queue.Queue()
    q.put((search_id,))
    upstream = []
    i = 0

    while not q.empty():
        n = q.get()
        if i > cuttoff_n:  # cuts off infinite loops. Number may need to be adjusted if adjoint catchments start to contain more than 200 individual regions
            break
        if isinstance(n, Iterable):
            for s in n:
                if s != -1:
                    upstream.append(s)
                if s in tree:
                    q.put(tree[s])
        else:
            if n != -1:
                upstream.append(n)
            if n in tree:
                q.put(tree[n])
        i += 1
    return upstream

def create_adjoint_dict(network_shp, out_file: str = None, stream_id_col: str = "COMID",
                        next_down_id_col: str = "NextDownID", order_col: str = "order_", trace_up: bool = True,
                        order_filter: int = 0):
    """
    Creates a dictionary where each unique id in a stream network is assigned a list of all ids upstream or downstream
    of that stream, as specified. By default is designed to trace upstream on GEOGloWS Delineation Catchment shapefiles,
    but can be customized for other files with column name parameters, customized to trace down, or filtered by stream
    order. If filtered by stream order, the dictionary will only contain ids of the given stream order, with the
    upstream or downstream ids for the other streams in the chain that share that stream order.
    Args:
        network_shp: path to  .shp file that contains the stream network. This file
                     must contain attributes for a unique id and a next down id, and if filtering by order number is
                     specified, it must also contain a column with stream order values.
        out_file: a path to an output file to write the dictionary as a .json, if desired.
        stream_id_col: the name of the column that contains the unique ids for the stream segments
        next_down_id_col: the name of the column that contains the unique id of the next down stream for each row, the
                          one that the stream for that row feeds into.
        order_col: name of the column that contains the stream order
        trace_up: if true, trace up from each stream, otherwise trace down.
        order_filter: if set to number other than zero, limits values traced to only ids that match streams with that
                      stream order
    Returns:
    """
    # Added to forgo computation on precreated json files, for testing
    if out_file is not None and os.path.exists(out_file):
        with open(out_file, 'r') as f:
            upstream_lists_dict = json.load(f)
        return upstream_lists_dict

    network_df = gpd.read_file(network_shp)
    columns_to_search = [stream_id_col, next_down_id_col]
    if order_filter != 0:
        columns_to_search.append(order_col)
    for col in columns_to_search:
        if col not in network_df.columns:
            print(f"Column {col} not present")
            return {}
    if trace_up:
        tree = make_tree_up(network_df, order_filter, stream_id_col, next_down_id_col, order_col)
    else:
        tree = make_tree_down(network_df, order_filter, stream_id_col, next_down_id_col, order_col)
    if order_filter != 0:
        upstream_lists_dict = {str(hydro_id): trace_tree(tree, hydro_id) for hydro_id in network_df[network_df[order_col] == order_filter][stream_id_col]}
    else:
        upstream_lists_dict = {str(hydro_id): trace_tree(tree, hydro_id) for hydro_id in network_df[stream_id_col]}
    if out_file is not None:
        if not os.path.exists(out_file):
            with open(out_file, "w") as f:
                json.dump(upstream_lists_dict, f, cls=NpEncoder)
        else:
            print("File already created")
            return upstream_lists_dict # Added to return dictionary anyways
    return upstream_lists_dict

def merge_streams(upstream_ids: list, network_gdf: gpd.GeoDataFrame):
    return network_gdf[network_gdf["LINKNO"].isin(upstream_ids)].dissolve()


def main(network_gpkg: str = '/Users/rchales/Data/NGA_delineation/Caribbean/TDX_streamnet_7020065090_01.shp', 
         output_gpkg_name: str=None):
    # DONE todo: make only 1 input which is the path to the geopackage/shapefile of the stream network
    # DONE todo: do not read json files, generate them in the script
    # DONE todo: make the output file name one of the parameters you can specify at the start of the script
    # DONE todo: make the readme.md file in the root of the repo
    # Make sure shapely is at least 2.0.1 to avoid access errors

    gdf = gpd.read_file(network_gpkg)
    out_json_path = os.path.splitext(network_gpkg)[0] + '_connectivity.json'

    allorders_dict = create_adjoint_dict(network_gpkg,
                    out_json_path,
                    stream_id_col="LINKNO",
                    next_down_id_col="DSLINKNO",
                    order_col="strmOrder")

    order_2_dict = create_adjoint_dict(network_gpkg,
                        stream_id_col="LINKNO",
                        next_down_id_col="DSLINKNO",
                        order_col="strmOrder",
                        order_filter=2)

    toporder2 = set([value[-1] for value in list(order_2_dict.values())])

    with Pool() as p:
        merged_features = p.starmap(merge_streams, [(allorders_dict[str(rivid)], gdf) for rivid in toporder2])

    # list all ids that were merged
    all_merged_rivids = [allorders_dict[str(rivid)] for rivid in toporder2]
    # turn a list of lists into a flat list
    all_merged_rivids = [item for sublist in all_merged_rivids for item in sublist]
    # remove duplicates
    all_merged_rivids = set(all_merged_rivids)

    # drop rivids that were merged
    gdf = gdf[~gdf["LINKNO"].isin(all_merged_rivids)]
    # concat the merged features
    gdf = pd.concat([gdf, *merged_features])

    if output_gpkg_name is None:
        output_gpkg_name = os.path.splitext(network_gpkg)[0] + '_connectivity.gpkg'

    gdf.to_file(output_gpkg_name, driver="GPKG")