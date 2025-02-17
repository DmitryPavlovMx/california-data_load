import os
import shutil
from os import path
from pathlib import Path
from functools import partial
from pyspark.sql.functions import regexp_replace,col,lit
from urllib.parse import urlsplit
import logging
from .settings import _SinaraSettings
from .substep import get_tmp_work_path

def get_block_size():
    if hasattr(_SinaraSettings, 'get_storage_block_size'):
        return _SinaraSettings.get_storage_block_size()
    else:
        return 10 * 1024 * 1024

def get_row_size():
    if hasattr(_SinaraSettings, 'get_storage_row_size'):
        return _SinaraSettings.get_storage_row_size()
    else:
        return 1024 * 1024
 
def SinaraArchive_save_file(file_col, tmp_entity_dir):
    '''
    _save_file defined as function since runtime error when declared as method:
    It appears that you are attempting to reference SparkContext from a broadcast variable, action, or transformation.
    SparkContext can only be used on the driver, not in code that it run on workers. For more information, see SPARK-5063.
    '''
    file_name = path.join(tmp_entity_dir, file_col.relPath.strip('/'))
    file_binary = file_col.content

    os.makedirs(path.dirname(file_name), exist_ok=True)
    with open(file_name, 'wb') as f_id:
        f_id.write(file_binary)
            
class SinaraArchive:
    """
    Provides effective way to store large files and pipeline entities in the SinaraML Storage.
    """

    BLOCK_SIZE = get_block_size()
    ROW_SIZE = get_row_size()
    
    def __init__(self, spark):
        self._spark = spark;
        
    def pack_files_from_tmp_to_spark_df(self, tmp_entity_dir):
        """
        Packs files from temporary directory to the Apache Spark dataframe.
        @param tmp_entity_dir - temporary directory with files to pack
        @return Apache Spark dataframe
        """

        tmp_url = tmp_entity_dir
        url = urlsplit(tmp_entity_dir)
        if not url.scheme:
            tmp_url = f'file://{url.path}'

        pathlist = [x for x in Path(tmp_entity_dir).glob(f'**/*') if not str(x.name).endswith(".parts") and not str(x.parent).endswith(".parts")]
        total_size = 0
        for path in pathlist:
            if Path(path).is_file():
                total_size = total_size + Path(path).stat().st_size
                if self.ROW_SIZE < Path(path).stat().st_size:
                    self._split_file(path, self.ROW_SIZE)
        cores = int(os.environ['SINARA_SERVER_CORES']) if 'SINARA_SERVER_CORES' in os.environ else 5
        threads = cores * 3
        partitions = int(total_size / self.BLOCK_SIZE) if int(total_size / self.BLOCK_SIZE) > threads else threads
            
        df = self._spark.read.format("binaryFile").option("pathGlobFilter", "*").option("recursiveFileLookup", "true") \
                .load(tmp_url) \
                .filter(col('length') <= self.ROW_SIZE) \
                .withColumn("relPath", regexp_replace('path', 'file:' + tmp_entity_dir, '')) \
                .drop("path")
        return df.repartition(partitions)

    # Deprecate erroreneous method name
    def pack_files_form_tmp_to_spark_df(self, tmp_entity_dir):
        """
        @Deprecated
        """
        logging.warning("pack_files_form_tmp_to_spark_df method is deprecated, use pack_files_from_tmp_to_spark_df instead")
        return self.pack_files_from_tmp_to_spark_df(tmp_entity_dir)
    
    def pack_files_from_tmp_to_store(self, tmp_entity_dir, store_path):
        """
        Packs files from temporary directory to store.
        @param tmp_entity_dir - temporary directory with files to pack
        @param store_path - path in the configured SinaraML store
        """
        df = self.pack_files_from_tmp_to_spark_df(tmp_entity_dir)
        df.write.option("parquet.block.size", self.BLOCK_SIZE).mode("overwrite").parquet(store_path)

    def pack(self, tmp_entity_dir, store_path):
        self.pack_files_from_tmp_to_store(tmp_entity_dir, store_path)
    
    def unpack_files_from_spark_df_to_tmp(self, df_archive, tmp_entity_dir):
        """
        Unpacks files from the Apache Spark dataframe to the temporary directory
        @param df_archive - Apache Spark dataframe with archived files
        @param tmp_entity_dir - temporary directory with files to unpack
        """
        df_archive.foreach(partial(SinaraArchive_save_file, tmp_entity_dir=tmp_entity_dir))
        self._join_parts(tmp_entity_dir)
    
    def unpack_files_from_store_to_tmp(self, store_path, tmp_entity_dir):
        """
        Unpacks files from the store to the temporary directory
        @param store_path - path in the configured SinaraML store
        @param tmp_entity_dir - temporary directory with files to unpack
        """
        df = self._spark.read.parquet(store_path)
        self.unpack_files_from_spark_df_to_tmp(df, tmp_entity_dir)

    def unpack(self, store_path):
        path = Path(store_path)
        tmp_entity_dir = Path(get_tmp_work_path()) / Path(store_path).name
        self.unpack_files_from_store_to_tmp(store_path, tmp_entity_dir)
        return str(tmp_entity_dir)
        
    def _split_file(self, path, chunk_size):
        parts_path = Path(f"{path.parent}/{path.name}.parts")
        parts_path.mkdir(parents=False, exist_ok=True)
        part_num = 0
        with open(path, 'rb') as fin:
            while True:
                chunk = fin.read(chunk_size)
                if not chunk: break
                chunk_filename = f"{parts_path}/part-{part_num:04d}"
                with open(chunk_filename, 'wb') as fout:
                    fout.write(chunk)
                    part_num = part_num + 1
        Path(f"{parts_path}/_PARTS").touch()
        
    def _join_parts(self, path):
        parts_dirlist = [x for x in Path(path).glob('**/*.parts') if x.is_dir()]
        for parts_dir in parts_dirlist:
            file_name = str(parts_dir.parent.joinpath(parts_dir.stem))
            with open(file_name, 'wb') as fout:
                for part_file in sorted(Path(parts_dir).glob('part-*')):
                    with open(str(part_file), "rb") as infile:
                        fout.write(infile.read())
            shutil.rmtree(parts_dir)