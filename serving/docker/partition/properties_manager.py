#
# Copyright 2023 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file
# except in compliance with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS"
# BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied. See the License for
# the specific language governing permissions and limitations under the License.
import logging
import os
import glob
import torch
import requests

# Properties to exclude while generating serving.properties
from utils import (is_engine_mpi_mode, get_download_dir, load_properties,
                   update_kwargs_with_env_vars)

EXCLUDE_PROPERTIES = [
    'option.model_id', 'option.save_mp_checkpoint_path', 'model_dir',
    'upload_checkpoints_s3url', 'properties_dir'
]


class PropertiesManager(object):

    def __init__(self, args, **kwargs):
        self.entry_point_url = None
        self.properties_dir = args.properties_dir
        self.properties = load_properties(self.properties_dir)
        self.properties |= update_kwargs_with_env_vars({})
        self.skip_copy = args.skip_copy

        if args.model_id:
            self.properties['option.model_id'] = args.model_id
        if args.engine:
            self.properties['engine'] = args.engine
        if args.save_mp_checkpoint_path:
            self.properties[
                'option.save_mp_checkpoint_path'] = args.save_mp_checkpoint_path
        if args.tensor_parallel_degree:
            self.properties[
                'option.tensor_parallel_degree'] = args.tensor_parallel_degree
        if args.pipeline_parallel_degree:
            self.properties[
                'option.pipeline_parallel_degree'] = args.pipeline_parallel_degree
        if args.quantize:
            self.properties['option.quantize'] = args.quantize

        if 'addl_properties' in kwargs:
            self.properties |= kwargs['addl_properties']

        self.is_mpi_mode = is_engine_mpi_mode(self.properties.get('engine'))

        self.set_and_validate_model_dir()

        self.set_and_validate_entry_point()
        self.set_and_validate_save_mp_checkpoint_path()

    def set_and_validate_model_dir(self):
        if 'model_dir' in self.properties:
            model_dir = self.properties['model_dir']
            model_files = glob.glob(os.path.join(model_dir, '*.bin'))
            model_files.append(
                glob.glob(os.path.join(model_dir, '*.safetensors')))
            if not model_files:
                raise ValueError(
                    f'No .bin or .safetensors files found in the dir: {model_dir}'
                )
        elif 'option.model_id' in self.properties:
            self.properties['model_dir'] = self.properties_dir
        else:
            model_files = glob.glob(os.path.join(self.properties_dir, '*.bin'))
            model_files.append(
                glob.glob(os.path.join(self.properties_dir, '*.safetensors')))
            if model_files:
                self.properties['model_dir'] = self.properties_dir
            else:
                raise ValueError(
                    f'No .bin or .safetensors files found in the dir: {self.properties_dir}'
                    '\nPlease specify the model_dir or model_id')

    def generate_properties_file(self):
        checkpoint_path = self.properties.get('option.save_mp_checkpoint_path')
        configs = {}

        for key, value in self.properties.items():
            if key not in EXCLUDE_PROPERTIES:
                if key == "option.entryPoint":
                    entry_point = self.properties.get("option.entryPoint")
                    if entry_point == "model.py":
                        continue
                    elif self.entry_point_url:
                        configs["option.entryPoint"] = self.entry_point_url
                else:
                    configs[key] = value

        properties_file = os.path.join(checkpoint_path, 'serving.properties')
        with open(properties_file, "w") as f:
            for key, value in configs.items():
                f.write(f"{key}={value}\n")

    def validate_tp_degree(self):
        tensor_parallel_degree = self.properties.get(
            'option.tensor_parallel_degree')
        if not tensor_parallel_degree:
            raise ValueError('Please specify tensor_parallel_degree')

        num_gpus = torch.cuda.device_count()
        if num_gpus < int(tensor_parallel_degree):
            raise ValueError(
                f'GPU devices are not enough to run {tensor_parallel_degree} partitions.'
            )

    def validate_pp_degree(self):
        pipeline_parallel_degree = self.properties.get(
            'option.pipeline_parallel_degree')
        if not pipeline_parallel_degree:
            raise ValueError(
                'Expecting pipeline_parallel_degree to be set of a default of 1'
            )

    def set_and_validate_entry_point(self):
        entry_point = self.properties.get('option.entryPoint')
        quantize = self.properties.get('option.quantize')
        if entry_point is None:
            entry_point = os.environ.get("DJL_ENTRY_POINT")
            if entry_point is None:
                entry_point_file = glob.glob(
                    os.path.join(self.properties_dir, 'model.py'))
                if entry_point_file:
                    self.properties['option.entryPoint'] = 'model.py'
                else:
                    engine = self.properties.get('engine')
                    if quantize:
                        pass
                    elif engine is None:
                        raise ValueError("Please specify engine")
                    elif engine.lower() == "python":
                        entry_point = "djl_python.transformers_neuronx"
                    else:
                        raise ValueError(f"Invalid engine: {engine}")
                    self.properties['option.entryPoint'] = entry_point
        elif entry_point.lower().startswith('http'):
            logging.info(f'Downloading entrypoint file.')
            self.entry_point_url = entry_point
            download_dir = get_download_dir(self.properties_dir,
                                            suffix='modelfile')
            model_file = os.path.join(download_dir, 'model.py')
            with requests.get(entry_point) as r:
                with open(model_file, 'wb') as f:
                    f.write(r.content)
            self.properties['option.entryPoint'] = model_file
            logging.info(f'Entrypoint file downloaded successfully')

    def set_and_validate_save_mp_checkpoint_path(self):
        save_mp_checkpoint_path = self.properties.get(
            "option.save_mp_checkpoint_path")
        if not save_mp_checkpoint_path:
            raise ValueError("Please specify save_mp_checkpoint_path")
        if save_mp_checkpoint_path.startswith("s3://"):
            self.properties[
                "upload_checkpoints_s3url"] = save_mp_checkpoint_path
            self.properties[
                "option.save_mp_checkpoint_path"] = get_download_dir(
                    self.properties_dir, "partition-model")
