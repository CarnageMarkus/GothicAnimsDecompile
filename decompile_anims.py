
from collections import defaultdict
from itertools import combinations
import json
import os
import shutil
from pathlib import Path

from zenkit import ModelAnimation, ModelScript

import convert_textures
import convert_model_hierarchy
import convert_model_scripts
import convert_model
import convert_model_mesh
import convert_morph_mesh
import convert_multiresolution_mesh
import convert_model_animations
import convert_worlds
import helpers

def rf(f, accuracy=4):
    return round(f, accuracy)


def find_latest_blender():
    system_disc_list = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'J']
    for system_disc in system_disc_list:
        blender_foundation_folder_path = Path(system_disc + ':/Program Files/Blender Foundation')
        if not blender_foundation_folder_path.exists():
            continue

        blender_folder_path_list = list(blender_foundation_folder_path.glob(f'*'))
        blender_folder_path_list = sorted([path for path in blender_folder_path_list if path.is_dir()])

        if len(blender_folder_path_list):
            return blender_folder_path_list[-1]

    return ''


def is_continuous_and_non_overlapping(ranges):
    # Sort by start
    ranges = sorted(ranges, key=lambda x: x.first_frame)
    for i in range(1, len(ranges)):
        prev = ranges[i - 1]
        curr = ranges[i]
        if curr.first_frame <= prev.last_frame:  # Overlap
            return False
        if curr.first_frame != prev.last_frame + 1:  # Gap
            return False
    return True


def find_best_anis_combo(asc_name, anis):
    if len(anis) == 1:
        return anis
    reason = "best combination"
    # Full range from all animations
    full_start = min(ani.first_frame for ani in anis)
    full_end = max(ani.last_frame for ani in anis)
    best_combo = []
    for r in range(2, len(anis) + 1):
        for combo in combinations(anis, r):
            if is_continuous_and_non_overlapping(combo):
                if len(combo) > len(best_combo):
                    best_combo = combo

    # no combo was found, find ani which uses biggest range
    if not best_combo:
        reason = "largest frame span"
        max_span = max(a.last_frame - a.first_frame for a in anis)
        best_combo = [a for a in anis if (a.last_frame - a.first_frame) == max_span]
        # if multiple anis cover the same range, try to disqualify some that are sped up using fps/speed modifiers
        if len(best_combo) > 1:
            notSpedUp = [a for a in best_combo if a.fps == 25.0 and a.speed == 0]
            if not notSpedUp:
                best_combo = [best_combo[0]]
            else:
                best_combo = [notSpedUp[0]]

    # best combo range
    best_combo_start = min(ani.first_frame for ani in best_combo)
    best_combo_end = max(ani.last_frame for ani in best_combo)

    remaining_anis = []
    if best_combo_start != full_start or best_combo_end != full_end:
        reason = "could not find combination covering full range"
        best_combo = anis
    
    if len(best_combo) != len(anis):
        remaining_anis = [ani for ani in anis if ani not in best_combo]

    print(f"Reconstruct: {asc_name}")
    print(f"-picked (reason: {reason}):")
    for ani in best_combo:
        print(f"  ani: {ani.name}, Range: {ani.first_frame}-{ani.last_frame}")
    if remaining_anis:
        print("-dropped:")
        for ani in remaining_anis:
            print(f"  ani: {ani.name}, Range: {ani.first_frame}-{ani.last_frame}")
    return best_combo


def parse_msb(model_script):
    # https://github.com/GothicKit/ZenKit/blob/main/src/ModelScript.cc

    ## collect all animations and group them by source ASC
    anis_by_asc = defaultdict(list)
    for ani in model_script.animations:
        if ani.model.lower().endswith('.asc'):
            anis_by_asc[ani.model].append(ani)
    
    ## find best ani combination that could result in full asc range without overlap
    final_anis = defaultdict(list)
    for asc_name, anis in anis_by_asc.items():
        final_anis[asc_name] = find_best_anis_combo(asc_name, anis)

    return final_anis


def parse_man(model_animation, mdh_dict):
    if model_animation.checksum != mdh_dict['checksum']:
        return None

    animation_data = {'checksum': model_animation.checksum,
                      'frame_count': model_animation.frame_count,
                      'fps': model_animation.fps,
                      'fps_source': model_animation.fps_source,
                      'layer': model_animation.layer,
                      'source_script': {}, 'frames': {}}

    bone_offset = 0
    for sample_index, sample in enumerate(model_animation.samples):
        if bone_offset >= len(model_animation.node_indices):
            bone_offset = 0

        bone_index = model_animation.node_indices[bone_offset]

        bone_name = mdh_dict['nodes'][bone_index]['name']
        if bone_name not in animation_data['frames']:
            animation_data['frames'][bone_name] = {}
            animation_data['frames'][bone_name]['translation'] = {}
            animation_data['frames'][bone_name]['rotation'] = {}

        translation = [sample.position.x, sample.position.y, sample.position.z]
        rotation = [sample.rotation.x, sample.rotation.y, sample.rotation.z, sample.rotation.w]

        translation = [rf(f) for f in translation]
        rotation = [rf(f) for f in rotation]

        animation_data['frames'][bone_name]['translation'][sample_index] = translation
        animation_data['frames'][bone_name]['rotation'][sample_index] = rotation

        bone_offset = bone_offset + 1

    return animation_data


def convert_anis(asc_name, mds_name, anis, man_file_paths, mdh_dict):
    model_animations = []
    for man_file in man_file_paths:
        model_animation = ModelAnimation.load(man_file)
        model_animations.append(model_animation)
    
    checksums = {ani.checksum for ani in model_animations}
    if len(checksums) != 1:
        print(f'ASC: {asc_name}, ani checksums are different. ABORT')
        return
    checksum = model_animations[0].checksum
    if model_animations[0].checksum not in mdh_dict:
        print(f'ASC: {asc_name}, could not find correct MDH. ABORT')
        return
    
    relative_path = man_file.relative_to(extract_path)
    mdh = mdh_dict[checksum]
    man_data_merged = {
        'hierarchy': mdh,
        'animation': {
            'checksum': checksum,
            'frame_count': model_animation.frame_count,
            'fps': model_animation.fps,
            'fps_source': model_animation.fps_source,
            'layer': model_animation.layer,
            'source_script': {},
            'frames': {}}}

    for model_animation in model_animations:
        man_data = parse_man(model_animation, mdh)
        for bone, tracks in man_data['frames'].items():
            if bone not in man_data_merged['animation']['frames']:
                man_data_merged['animation']['frames'][bone] = {
                    'translation': {},
                    'rotation': {}}
            for trackName, trackFrames in tracks.items():
                for frame, values in trackFrames.items():
                    for v in values:
                        man_data_merged['animation']['frames'][bone][trackName][frame]=v
        
    save_path = intermediate_path / (str(relative_path) + '.json')
    save_path.parent.mkdir(exist_ok=True, parents=True)

    json_data = json.dumps(man_data, indent=4, ensure_ascii=False, sort_keys=False, default=str)
    save_path.write_text(json_data, encoding='utf-8')

    print(f'prepared: {relative_path}')
    helpers.run_blender(blender_executable_file_path, blender_script_file_path)
    print(f'[MODEL ANIMATION] End convert MAN via blender')


extract_path = None,
intermediate_path = None
convert_path = None
blender_executable_file_path = None

def convert():
    global extract_path, intermediate_path, convert_path, blender_executable_file_path
    config_file_path = Path('config.json')
    if not config_file_path.exists():
        print(f'ERROR: can\'t find config file [{config_file_path}].')
        return
    config_data = config_file_path.read_text()
    config = json.loads(config_data)

    extract_path = Path(config['extract_folder'])
    if not extract_path.is_absolute():
        extract_path = Path.cwd() / extract_path
    intermediate_path = Path(config['intermediate_folder'])
    if not intermediate_path.is_absolute():
        intermediate_path = Path.cwd() / intermediate_path
    convert_path = Path(config['convert_folder'])
    if not convert_path.is_absolute():
        convert_path = Path.cwd() / convert_path

    if not extract_path.exists():
        print(f'ERROR: folder "{extract_path}" not exist!')
        return

    blender_executable_file_path = Path(config['blender_folder']) / 'blender.exe'
    if not blender_executable_file_path.exists():
        blender_folder = find_latest_blender()
        blender_executable_file_path = Path(blender_folder) / 'blender.exe'
        if not blender_executable_file_path.exists():
            print(f'ERROR: can\'t find blender executable file.')
            return
        else:
            print(f'WARNING: Blender folder don\'t setup in config, used blender with path: {blender_folder}')

    shutil.rmtree(intermediate_path, ignore_errors=True)
    intermediate_path.mkdir(parents=True, exist_ok=True)

    shutil.rmtree(convert_path, ignore_errors=True)
    convert_path.mkdir(parents=True, exist_ok=True)

    # convert hierarchies
    convert_model_hierarchy.convert(extract_path, intermediate_path, convert_path)
    
    mdh_files = list(Path(intermediate_path).rglob(f'*.MDH.json'))
    mdh_by_checksum = defaultdict(list)
    for mdh_file in mdh_files:
        mdh_data = mdh_file.read_text()
        mdh_dict = json.loads(mdh_data)
        checksum = mdh_dict['checksum']
        mdh_by_checksum[checksum] = mdh_dict

    ## 
    msb_file_path_list = list(Path(extract_path).rglob(f'*.MSB'))

    for msb_file_path in msb_file_path_list:
        msb_folder_path = msb_file_path.parent
        man_file_path_list = list(Path(msb_folder_path).rglob(f'*.MAN'))
        mds_name = msb_file_path.stem.upper().split('_')[0]
        model_script = ModelScript.load(msb_file_path)
        anis_by_asc_dict = parse_msb(model_script)
        #relative_path = msb_file_path.relative_to(extract_path)
        #save_path = convert_path / (str(relative_path) + '.json')
        #save_path.parent.mkdir(exist_ok=True, parents=True)

        # convert multiple anis to one asc
        for asc_name, anis in anis_by_asc_dict.items():
            ani_names = {ani.name for ani in anis}
            man_files = [
                path for path in man_file_path_list
                if path.stem in ani_names
            ]
            convert_anis(asc_name, mds_name, anis, man_files, mdh_by_checksum)
            

if __name__ == '__main__':
    convert()
