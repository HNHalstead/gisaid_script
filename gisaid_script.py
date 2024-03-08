#! /usr/bin/python

import argparse
import logging
import os
import pathlib
import re
import shlex
import subprocess
import sys
import numpy as np
import pandas as pd
from Bio import SeqIO
from functools import partial
from glob import glob
from datetime import datetime
from IPython.display import display
from logging.handlers import RotatingFileHandler
from typing import Tuple

# from tqdm import tqdm


parser = argparse.ArgumentParser()
parser.add_argument(help="Your GISAID submitter ID", dest="submitter")
parser.add_argument(
    "-i",
    "--indir",
    help=(
        "Path to directory containing input tables "
        "(mutually exclusive with '--terra' and '--dashboard' options)"
    ),
    type=str,
    dest="indir",
    default=os.getcwd(),
)
parser.add_argument(
    "-t",
    "--terra",
    help=(
        "File path to the table of results from Terra; "
        "can be a list, with each item preceded by the "
        "'-t' or '--terra' flag."
    ),
    nargs="*",
    dest="terra_table",
    default=None,
)
parser.add_argument(
    "-d",
    "--dashboard",
    help="File path to the table of metadata from the WAPHL dashboard",
    nargs="*",
    dest="dashboard_table",
    default=None,
)
parser.add_argument(
    "-v",
    "--vocs",
    help=(
        "File path to a list of variants of concern/interest. "
        "File should have two columns: VOC and VOI, named "
        "on the first line, with Nextclade clade designations "
        "or Pango Lineages listed underneath for variants of "
        "concern and interest, respectively"
    ),
    type=str,
    dest="voc_list",
    default=None,
)
parser.add_argument(
    "-o",
    "--outdir",
    help=(
        "Path to directory to which outputs should be written; "
        "default is the '--indir', if provided, or else the "
        "current working dir"
    ),
    type=str,
    dest="outdir",
    default=os.getcwd(),
)
parser.add_argument(
    "-w",
    "--workflow",
    help=("Workflow used to generate Terra table: 'titan' or 'lang'"),
    type=str,
    dest="workflow",
    default="titan",
)
parser.add_argument(
    "-g",
    "--gsutil",
    help=("Absolute path to gsutil tool, in case not in active PATH"),
    type=str,
    dest="gsutil_path",
    default="gsutil",
)
parser.add_argument(
    "-s",
    "--skip",
    help=(
        "Skip downloading assemblies; assumes they're present in this dir "
        "in a subdir named 'assemblies'"
    ),
    type=bool,
    dest="skip_assembly_download",
    default=False,
)
parser.add_argument(
    "--no_auto_qc",
    help=("If TRUE, ignore genome QC criteria in generating outputs"),
    type=bool,
    dest="no_auto_qc",
    default=False,
)
parser.add_argument(
    "--author_list",
    help=(
        "Absolute path to file containing a list of Authors to be attributed "
        "in repositories.  Names should be on a single line, separated by semicolons."
    ),
    type=str,
    dest="author_list",
)

user_args = vars(parser.parse_args())
INDIR = user_args.get("indir")


# For ease of use, look for the files in INDIR
for key in ("terra_table", "dashboard_table"):
    if user_args[key] is None:
        key_part = key.split("_")[0]
        keypattern = f"*{key_part}*"
        matches = glob(os.path.join(INDIR, keypattern))
        if len(matches) > 0:
            user_args[key] = matches
        else:
            missing_input_message = (
                f"Could not find required input {key_part}. "
                f"Please either provide with --{key_part} flag, "
                f"or ensure that file with '{key_part}' in file name "
                f"is present in {INDIR}."
            )
            print(missing_input_message)
            sys.exit()


SUBMITTER = user_args.get("submitter")
TERRA_TABLE = user_args.get("terra_table")
DASHBOARD_TABLE = user_args.get("dashboard_table")
OUTDIR = user_args.get("outdir")
VOC_LIST = user_args.get("voc_list")
GSUTIL_PATH = user_args.get("gsutil_path")
NO_AUTO_QC = user_args.get("no_auto_qc")
WORKFLOW = user_args.get("workflow").lower()
SKIP_ASSEMBLY_DOWNLOAD = user_args.get('skip_assembly_download')

ASSEMBLY_DIR = os.path.join(OUTDIR, "assemblies")
EXTENSION_HANDLERS = {
    ".csv": pd.read_csv,
    ".tsv": partial(pd.read_csv, sep="\t"),
    ".txt": partial(pd.read_csv, sep="\t"),
    ".xls": partial(pd.read_excel, engine="xlrd"),
    ".xlsx": partial(pd.read_excel, engine="openpyxl"),
}
TODAY = datetime.now().strftime("%Y-%m-%d")
# HNH_edit
DEFAULT_AUTHORS = (
            "Drew MacKellar, Philip Dykema, "
            "Holly Halstead, Kristen Waterman, "
            "Abigail Hicks, JohnAric Peterson, "
            "Andrew Delgado, Sarah Krantz "
)
AUTHORS_PATH = user_args.get('author_list')
if AUTHORS_PATH:
    with open(AUTHORS_PATH, 'r') as f:
        AUTHORS = [name.strip() for name in f.readline().split(';')]
else:
    AUTHORS = DEFAULT_AUTHORS


def setup_logger(output_dir: str) -> logging.Logger:
    """Returns a logger object writing to 'gisaid_script_logs.txt'."""
    log_filename = "gisaid_script_logs.txt"
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    logger = logging.getLogger("gisaid_script_logger")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_filename)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())

    return logger


def load_tables(table_list: list, terra_table=False) -> pd.DataFrame:
    """Load input tables and consolidate into pandas DataFrames"""

    def load_single_table(filepath):
        """Attempt to determine whether a given file is
        TSV, CSV, or Excel, and return the given table as
        a pandas DataFrame"""
        _, ext = os.path.splitext(filepath)
        df = EXTENSION_HANDLERS.get(ext, pd.read_excel)(filepath)
        return df

    df_list = list()
    for table in table_list:
        single_df = load_single_table(table)
        if terra_table:
            cols = single_df.columns.copy().tolist()
            cols[0] = "sample_name"
            single_df.columns = cols
        df_list.append(single_df)
    df = pd.concat(df_list)
    return df


def merge_tables(
    terra_df: pd.DataFrame, dashboard_df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """Merges the Dashboard and Terra tables, and reformats slightly"""
    pattern = ".*(WA[0-9]{7}).*"
    terra_df["wa_no"] = terra_df["sample_name"].str.extract(pattern)
    missing_wa_nos = terra_df[terra_df["wa_no"].isna()]["sample_name"].tolist()
    if len(missing_wa_nos) > 0:
        missing_wa_nos_msg = "\n".join(
            [
                ("No WA number could be determined for the following " "samples: "),
                *missing_wa_nos,
                "and they will be omitted from the outputs",
            ]
        )
        logger.warning(missing_wa_nos_msg)
        print()
    terra_df.dropna(subset=["wa_no"], inplace=True)

    dashboard_df.columns = [
        "_".join(col.lower().split()) for col in dashboard_df.columns
    ]

    merged_df = pd.merge(
        terra_df, dashboard_df, left_on="wa_no", right_on="specimenid", how="left"
    )
    # "sample_id" field required by PHA4GE standard, later
    merged_df['sample_id'] = merged_df['seq_id'].fillna('').str.split('/', expand=True).iloc[:, 2]

    return merged_df


def get_column_map(workflow: str) -> dict:
    cols_needed = (
        "sequence",
        "coverage",
        "ivar_version",
        "nextclade_clade",
        "pangolin_lineage",
    )
    titan_col_names = (
        "assembly_fasta",
        "percent_reference_coverage",
        "ivar_version_consensus",
        "nextclade_clade",
        "pango_lineage",
    )
    lang_col_names = (
        "consensus_seq",
        "coverage_trim",
        "ivar_version_consensus",
        "nextclade_clade",
        "pangolin_lineage",
    )
    workflow_cols = dict()
    for key, col_names in zip(("titan", "lang"), (titan_col_names, lang_col_names)):
        workflow_cols[key] = {
            col_needed: col_name for col_needed, col_name in zip(cols_needed, col_names)
        }
    return workflow_cols[workflow]


def auto_qc(merged_df: pd.DataFrame, logger: logging.Logger) -> list:
    conditions = merged_df[col_names.get("coverage")] < 60
    key_metrics = [col_names.get("coverage")]
    bad_samples = merged_df[conditions]["wa_no"].dropna().tolist()
    if len(bad_samples) > 0:
        auto_qc_msg = "\n".join(
            [
                (
                    "The following samples failed to meet the minimum QC metrics "
                    f"set for genome quality (currently {key_metrics})"
                ),
                *bad_samples,
                "and will be omitted from the outputs",
            ]
        )
        logger.warning(auto_qc_msg)
        print()
    return bad_samples


def download_assemblies(merged_df: pd.DataFrame) -> Tuple[dict, dict]:
    """For each sample represented in the Terra results, attempts to
    download the corresponding genome assembly"""

    pathlib.Path(ASSEMBLY_DIR).mkdir(exist_ok=True, parents=True)
    pipes = {key: subprocess.PIPE for key in ("stdout", "stderr")}
    download_stdouts, download_stderrs = dict(), dict()

    def gsutil_download(row):
        wa_no, url = row[0], row[1]
        cmd = f"{GSUTIL_PATH} cp {url} {ASSEMBLY_DIR}"
        proc = subprocess.Popen(shlex.split(cmd), **pipes)
        stdout, stderr = proc.communicate()
        download_stdouts.update({wa_no: stdout.decode("utf-8")})
        download_stderrs.update({wa_no: stderr.decode("utf-8")})

    _ = merged_df[["wa_no", col_names.get("sequence")]].apply(gsutil_download, axis=1)

    return download_stdouts, download_stderrs


def handle_counties(county: str):
    """Ensure that any fields reported for the County in which sample
    was collected are validly named WA counties; else return just state
    for localization field in metadata"""
    wa_counties_lower = (
        "adams; asotin; benton; chelan; clallam; clark; columbia; cowlitz; "
        "douglas; ferry; franklin; garfield; grant; grays harbor; island; "
        "jefferson; king; kitsap; kittitas; klickitat; lewis; lincoln; mason; "
        "okanogan; pacific; pend oreille; pierce; san juan; skagit; skamania; "
        "snohomish; spokane; stevens; thurston; wahkiakum; walla walla; "
        "whatcom; whitman; yakima"
    ).split("; ")
    no_county = "North America / USA / Washington"
    if county.lower() in wa_counties_lower:
        words = [word.capitalize() for word in county.split()]
        new_county = (" ".join(words) if len(words) > 1 else words[0])
        return_str = f"{no_county} / {new_county} County"
    else:
        return_str = no_county
    return return_str


def get_platform(sample_index: str) -> str:
    instrument_type = "Illumina NextSeq"
    miseqs = ["M4796", "M5130", "M5916"]
    for miseq in miseqs:
        if miseq in sample_index:
            instrument_type = "Illumina MiSeq"
    return instrument_type


def get_collecting_lab_address(lab_name: str) -> str:
    """Fetch the address of the submitting lab, if available"""
    lab_addr_map = {
        'atlas genomics': (
            '2296 W. Commodore Way, Suite 220, Seattle, WA 98199, USA'
        ),
        'confluence': (
            '1201 South Miller St Wenatchee, WA 98801, USA'
        ),
        'incyte diagnostics spokane': (
            '15912 East Marietta Avenue, Suite 200, '
            'Spokane Valley, WA 99216, USA'
        ),
        'interpath laboratory': (
            '8660 Emerald St # 102, Boise, ID 83704, USA'
        ),
        'northwest laboratory': (
            '3548 Meridian St, Suite 101, Bellingham, WA 98225, USA'
        ),
    }
    pha4ge_collecting_lab_addr = lab_addr_map.get(
        lab_name.lower(), 'WA, USA'
    )
    return pha4ge_collecting_lab_addr


def handle_reasons(reason: str) -> str:
    """Convert WAPHL categories of reasons for sequencing to those that fit PHA4GE standard"""
    reasons_map = {
        "phl diagnostic": "Baseline surveillance (random sampling)",
        "suspected vaccine breakthrough": "Vaccine escape surveillance",
        "suspected reinfection": "Re-infection surveillance",
        "outbreak investigation": "Cluster/Outbreak investigation",
        "travel associated": "Travel-associated surveillance",
        "other": "Not Provided",
        "s-dropout": "Screening for Variants of Concern (VOC)",
        "sentinel surveillance": "Baseline surveillance (random sampling)",
        "pt": "DROP",
    }
    pha4ge_reason = reasons_map.get(
        reason.lower(), "NO_REASON_FOUND"
    )
    return pha4ge_reason


def get_pha4ge_metadata(df: pd.DataFrame, logger: logging.Logger) -> pd.DataFrame:
    """Generates the proper field names & formats for metadata
    compatible with the PHA4GE standard."""
    pha4ge_map = {
        'specimen_collector_sample_id': df["sample_id"],
        'bioproject_umbrella_accession': "PRJNA615625",
        'bioproject_accession': "PRJNA749781",
        'biosample_accession': None,
        'genbank_ena_ddbj_accession': None,
        'gisaid_accession': None,
        'gisaid_virus_name': df["seq_id"],
        'sample_collected_by': df["submittinglab"],
        'sample_collector_contact_address': (
            df['submittinglab'].fillna("").apply(get_collecting_lab_address)
        ),
        'sequence_submitted_by': "Washington State Department of Health Public Health Laboratories",
        'sequence_submitter_contact_address': "1610 NE 150th St., Shoreline, WA 98155",
        'sample_collection_date': df["collected_date"],
        'geo_loc_name_country': "USA",
        'geo_loc_name_state_province_territory': "Washington",
        'geo_loc_name_county_region': df["county"].fillna("").apply(handle_counties),
        'organism': "Severe acute respiratory syndrome coronavirus 2",
        'isolate': df[["sample_id", "collected_date"]].apply(
            lambda x: f"SARS-CoV-2/Homo sapiens/USA/{x[0]}/{x[1]}",
            axis=1
        ),
        'host_scientific_name': "Homo sapiens",
        'host_disease': "COVID-19",
        'purpose_of_sequencing': df["reason"].apply(handle_reasons),
        'sequencing_instrument': df["sample_name"].apply(get_platform),
        'sequencing_protocol_name': "Illumina COVIDSeq",
        'raw_sequence_data_processing_method': "FASTQC, Trimmomatic: quality and adapter trimming",
        'dehosting_method': "NCBI SRA human scrubber",
        'consensus_sequence_software_name': "iVar",
        'consensus_sequence_software_version': (
            df["ivar_version_consensus"].fillna(" . . Unknown").str.split(expand=True).iloc[:, -1]
        ),
        'breadth_of_coverage_value': df["percent_reference_coverage"].fillna("0").apply(lambda x: f"{int(x)}%"),
        'depth_of_coverage_value': df["assembly_mean_coverage"].fillna("0").apply(lambda x: f"{int(x)}x"),
        'bioinformatics_protocol': (
            "https://github.com/theiagen/public_health_viral_genomics/blob/main/workflows/wf_titan_illumina_pe.wdl"
        ),
    }

    pha4ge_metadata_df = pd.DataFrame(pha4ge_map)

    pha4ge_metadata_df.dropna(
        axis=0, subset=['specimen_collector_sample_id'], inplace=True
    )

    return pha4ge_metadata_df


def get_gisaid_metadata(pha4ge_metadata_df: pd.DataFrame, logger: logging.Logger) -> pd.DataFrame:
    """Configure output metadata spreadsheet for upload to GISAID repository
    from input PHA4GE standard"""
    gisaid_fields = {
        ("submitter", "Submitter"): SUBMITTER,
        ("fn", "FASTA filename"): "all_sequences.fasta",
        ("covv_virus_name", "Virus name"): pha4ge_metadata_df["gisaid_virus_name"],
        ("covv_type", "Type"): "betacoronavirus",
        ("covv_passage", "Passage details/history"): "Original",
        ("covv_collection_date", "Collection date"): pha4ge_metadata_df["sample_collection_date"],
        ("covv_location", "Location"): pha4ge_metadata_df["geo_loc_name_county_region"],
        ("covv_add_location", "Additional location information"): None,
        ("covv_host", "Host"): "Human",
        ("covv_add_host_info", "Additional host information"): None,
        ("covv_sampling_strategy", "Sampling Strategy"): pha4ge_metadata_df['purpose_of_sequencing'],
        ("covv_gender", "Gender"): "unknown",
        ("covv_patient_age", "Patient age"): "unknown",
        ("covv_patient_status", "Patient status"): "unknown",
        ("covv_specimen", "Specimen source"): None,
        ("covv_outbreak", "Outbreak"): None,
        ("covv_last_vaccinated", "Last vaccinated"): None,
        ("covv_treatment", "Treatment"): None,
        ("covv_seq_technology", "Sequencing technology"): pha4ge_metadata_df["sequencing_instrument"],
        ("covv_assembly_method", "Assembly method"): (
            pha4ge_metadata_df['consensus_sequence_software_version'].apply(lambda x: f"iVar v{x}")
        ),
        ("covv_coverage", "Coverage"): pha4ge_metadata_df['depth_of_coverage_value'],
        (
            "covv_orig_lab",
            "Originating lab",
        ): pha4ge_metadata_df['sample_collected_by'],
        ("covv_orig_lab_addr", "Address"): (
            pha4ge_metadata_df['sample_collector_contact_address']
        ),
        (
            "covv_provider_sample_id",
            "Sample ID given by originating laboratory"
        ): None,
        (
            "covv_subm_lab",
            "Submitting lab",
        ): "Washington State Department of Health Public Health Laboratories",
        ("covv_subm_lab_addr", "Address"): "1610 NE 150th St., Shoreline, WA 98155",
        ("covv_subm_sample_id", "Sample ID given by the submitting laboratory"): None,
        ("covv_authors", "Authors",): AUTHORS,
        ("covv_comment", "Comment"): None,
        ("comment_type", "Comment Icon"): None,
    }

    gisaid_metadata_df = (
        pd.DataFrame(gisaid_fields)
        .sort_values(("covv_virus_name", "Virus name"))
        .reset_index(drop=True)
    )
    # new_output_df.dropna(
    #     axis=0, subset=[("covv_virus_name", "Virus name")], inplace=True
    # )
    # HNH Edit
    gisaid_metadata_df["covv_consortium"] = ""
    return gisaid_metadata_df


def format_collection_date(date_col: pd.Series) -> pd.Series:
    """Reformat collection dates from MM/DD/YYYY in LIMS Dashboard output to
    YYYY-MM-DD format required by NCBI."""
    formatted_date_col = pd.to_datetime(date_col, format='%Y-%m-%d')
    return formatted_date_col


def format_ictv_isolate(specimen_col: pd.Series) -> pd.Series:
    """Reformat pha4ge specimen_collector_sample_id into
    ISTC format isolate name"""
    formatted_specimen_col = specimen_col.apply(
        lambda x: f"SARS-CoV-2/Human/USA/{x}/{TODAY[:4]}"
    )
    return formatted_specimen_col


def get_biosample_metadata(pha4ge_metadata_df: pd.DataFrame, logger: logging.Logger) -> pd.DataFrame:
    """Configure output metadata spreadsheet for upload to NCBI BioSample
    database from input PHA4GE standard"""
    biosample_fields = {
        'sample_name': pha4ge_metadata_df['specimen_collector_sample_id'],
        'bioproject_accession': pha4ge_metadata_df['bioproject_accession'],
        # 'umbrella_bioproject_accession': pha4ge_metadata_df['bioproject_umbrella_accession'],
        'organism': pha4ge_metadata_df['organism'],
        'collected_by': pha4ge_metadata_df['sample_collected_by'],
        'collection_date': format_collection_date(pha4ge_metadata_df['sample_collection_date']),
        'geo_loc_name': (
            pha4ge_metadata_df['geo_loc_name_county_region'].str.split(' / ', expand=True).iloc[:, 1:]
            .fillna('').agg(': '.join, axis=1).str.strip(': ')
        ),
        'host': pha4ge_metadata_df['host_scientific_name'],
        'host_disease': pha4ge_metadata_df['host_disease'],
        'isolate': format_ictv_isolate(
            pha4ge_metadata_df['specimen_collector_sample_id']
        ),
        'isolation_source': 'Clinical/Nasal Swab',
        # Going to have to leave out the GISAID accession field for now;
        # can't figure out how to easily accommodate that within this script
        # 'gisaid_accession': pha4ge_metadata_df['gisaid_accession'],
        'gisaid_virus_name': pha4ge_metadata_df['gisaid_virus_name'],
        'purpose_of_sequencing': pha4ge_metadata_df['purpose_of_sequencing'],
        'sequenced_by': pha4ge_metadata_df['sequence_submitted_by'],
    }
    biosample_metadata_df = pd.DataFrame(biosample_fields)
    biosample_metadata_df.rename(columns={
        'umbrella_bioproject_accession': 'bioproject_accession',
    },
    inplace=True
    )
    return biosample_metadata_df


def get_genbank_metadata(pha4ge_metadata_df: pd.DataFrame, logger: logging.Logger) -> pd.DataFrame:
    """Configure output metadata spreadsheet for upload to GenBank repository
    from input PHA4GE standard"""
    genbank_fields = {
        'sequence_ID': pha4ge_metadata_df['specimen_collector_sample_id'],
        'isolate': format_ictv_isolate(
            pha4ge_metadata_df['specimen_collector_sample_id']
        ),
        'collection-date': format_collection_date(
            pha4ge_metadata_df['sample_collection_date']
        ),
        'host': 'Homo sapiens',
        'country': 'USA: Washington',
        'isolation-source': 'Nasal swab',
        'BioProject Accession': pha4ge_metadata_df['bioproject_accession'],
        'BioSample Accession': pha4ge_metadata_df['biosample_accession'],
    }
    genbank_metadata_df = pd.DataFrame(genbank_fields)
    return genbank_metadata_df



def generate_fasta(df: pd.DataFrame, logger: logging.Logger):
    """Gather assemblies and output with new header lines"""
    file_df = (
        df[["wa_no", "seq_id", col_names.get("sequence")]]
        .copy()
        .sort_values("seq_id")
    )

    seq_id_pattern = r".*/(?:call-consensus|cacheCopy)/(.*)"
    file_df["consensus_file"] = (
        ASSEMBLY_DIR
        + os.sep
        + file_df[col_names.get("sequence")].fillna("").str.extract(seq_id_pattern)
    )
    fasta_generation_errs = dict()

    def gather_seqs(row, out_buffer):
        try:
            with open(row["consensus_file"], "r") as seq_buffer:
                rec = next(SeqIO.parse(seq_buffer, "fasta"))
                rec.id = row["seq_id"]
                rec.description = ""
                SeqIO.write(rec, out_buffer, "fasta")
        except (TypeError, AttributeError, FileNotFoundError) as err:
            fasta_generation_errs[row["wa_no"]] = err
            pass

    all_seq_path = os.path.join(OUTDIR, "all_sequences.fa")
    with open(all_seq_path, "w") as out_buffer:
        _ = file_df.dropna(subset=["wa_no"]).apply(
            gather_seqs, axis=1, out_buffer=out_buffer
        )
    logger.info(f"Consolidated genome assemblies written to {all_seq_path}")
    if len(fasta_generation_errs) > 0:
        fasta_generation_msg = "\n".join(
            [
                (
                    "There were problems with gathering/renaming the "
                    "genome sequences for the following samples, and they "
                    "were omitted from the outputs:"
                ),
                *fasta_generation_errs.keys(),
            ]
        )
        logger.warning(fasta_generation_msg)
        print()
    return fasta_generation_errs


def handle_missing_data(df: pd.DataFrame, req_fields: list, logger: logging.Logger):
    samples_missing_data = list()
    working_df = df.copy().set_index("wa_no")[req_fields]  # .astype(str)
    for req_field in req_fields:
        missing_mask = working_df[req_field].replace("", None).isna()
        samples_missing_data.extend(working_df[missing_mask].index.astype(str).tolist())
    if len(samples_missing_data) > 0:
        missing_data_msg = "\n".join(
            [
                (
                    "The following samples are missing data in the required "
                    f"fields {req_fields}:"
                ),
                *samples_missing_data,
                ("and will be omitted from the outputs."),
            ]
        )
        logger.warning(missing_data_msg)
        print()
    return samples_missing_data


def handle_missing_genomes(
    df: pd.DataFrame, download_stderrs: dict, logger: logging.Logger
):
    """Detects errors that occur when downloading genomes,
    and prints/logs messages warning the user they will be
    omitted from the outputs."""

    download_failures = list()
    download_failure_msgs = list()

    for sample, msg in download_stderrs.items():
        if ("AccessDeniedException" in msg) or ("CommandException" in msg):
            download_failures.append(sample)
            download_failure_msgs.append(msg)

    if len(download_failures) > 0:
        download_failures_msg = "\n".join(
            [
                "NOTE: the following sequences could not "
                "be downloaded successfully and will be omitted "
                "from the outputs: ",
                "\n".join(sample for sample in download_failures),
            ]
        )
        logger.warning(download_failures_msg)
        print()
    failure_msgs = " ".join(download_failure_msgs)
    if "AccessDeniedException" in failure_msgs:
        access_msg = (
            "Cannot access the stored object(s); "
            "please run 'gsutil config' before re-running "
            "script."
        )
        logger.warning(access_msg)
        print()
    elif ("CommandException" in failure_msgs) or ("Error" in failure_msgs):
        url_msg = (
            f"Please check formatting of '{col_names.get('sequence')}' "
            "column in input Terra tables for samples "
            "listed above."
        )
        logger.warning(url_msg)
        print()

    return download_failures


def get_vocs():
    if VOC_LIST:
        _, voc_ext = os.path.splitext(VOC_LIST)
        voc_df = EXTENSION_HANDLERS.get(voc_ext)(VOC_LIST)
        voc_values = voc_df.T.values
        vocs, vois = voc_values[0], voc_values[1]
    else:
        vocs, vois = [], []
    return vocs, vois


def handle_vocs(vocs: list, vois: list, terra_df: pd.DataFrame, logger: logging.Logger):
    if len(vocs) == 0 and len(vois) == 0:
        return [], []
    clades = (
        terra_df[
            [
                "wa_no",
                col_names.get("nextclade_clade"),
                col_names.get("pangolin_lineage"),
            ]
        ]
        .dropna()
        .set_index("wa_no")[
            [col_names.get("nextclade_clade"), col_names.get("pangolin_lineage")]
        ]
    )
    voc_samples = clades[
        (clades[col_names.get("nextclade_clade")].isin(vocs))
        | (clades[col_names.get("pangolin_lineage")].isin(vocs))
    ]
    voi_samples = clades[
        (clades[col_names.get("nextclade_clade")].isin(vois))
        | (clades[col_names.get("pangolin_lineage")].isin(vois))
    ]

    vocs_msg, vois_msg = None, None
    for sample_df, label, list_, msg in zip(
        (voc_samples, voi_samples), ("VOC", "VOI"), (vocs, vois), (vocs_msg, vois_msg)
    ):
        if sample_df.shape[0] > 0:
            samples = sample_df.index.values.tolist()
            msg = "\n".join(
                [
                    (
                        "The following samples were found to be in the designated "
                        f"{label} list ({list_}): "
                    ),
                    *samples,
                    (
                        "Please notify the Epidemiologist group at "
                        "'wgs-epi@doh.wa.gov' prior to upload to GISAID"
                    ),
                ]
            )
            logger.info(msg)
            print()
    # Kind of janky solution to show which sample is which lineage:
    # display in terminal, then write to separate TSV file
    vocs_vois_df = pd.concat([voc_samples, voi_samples])
    if vocs_vois_df.shape[0] > 0:
        outpath = os.path.join(OUTDIR, "vocs_vois_table.tsv")
        vocs_vois_df.to_csv(outpath, sep="\t")
        vocs_vois_out_msg = (
            "The following table of samples "
            "and Pango Linage/NextClade Clade "
            f"was written to {outpath}."
        )
        logger.info(vocs_vois_out_msg)
        display(vocs_vois_df)
        print()

    return voc_samples, voi_samples


def main():
    """Run the functions of this script in order, to process data in
    preparation for uploading to GISAID"""
    print()
    pathlib.Path(OUTDIR).mkdir(exist_ok=True)
    logger = setup_logger(OUTDIR)
    dashboard_df = load_tables(DASHBOARD_TABLE)
    terra_df = load_tables(TERRA_TABLE, terra_table=True)
    global col_names
    col_names = get_column_map(WORKFLOW)

    for df, path in zip((dashboard_df, terra_df), (DASHBOARD_TABLE, TERRA_TABLE)):
        if df.shape[0] < 1:
            bad_input_message = (
                "Could not interpret or could not find appropriate data "
                f"in {path}; please check format of input file and try again."
            )
            logger.critical(bad_input_message)
            sys.exit()

# DCM 20210805 note: the following block is just to get
# a local copy of the `merged_df` table to look at
    merged_df = merge_tables(terra_df, dashboard_df, logger=logger)
    merged_df_path = os.path.join(OUTDIR, 'merged_df.tsv')
    merged_df.to_csv(merged_df_path, sep='\t')
    # sys.exit()

    req_fields = ["collected_date"]
    samples_missing_data = handle_missing_data(merged_df, req_fields, logger)
    vocs, vois = get_vocs()
    _, _ = handle_vocs(vocs, vois, merged_df, logger)
    if not NO_AUTO_QC:
        bad_samples = auto_qc(merged_df, logger)
    else:
        bad_samples = []

    # Note: the assembly download execution step is the most
    # Costly & time-intensive; comment out lines below if they're already present
    print(
        "Downloading consensus genome assemblies "
        "from the cloud; this may take some time...",
        end="\n",
    )
    failed_samples = samples_missing_data + bad_samples

    if SKIP_ASSEMBLY_DOWNLOAD:
        download_stderrs = dict()
    else:
        _, download_stderrs = download_assemblies(
            merged_df[~merged_df["wa_no"].isin(failed_samples)]
        )

    missing_genomes = handle_missing_genomes(merged_df, download_stderrs, logger)
    failed_samples.extend(missing_genomes)
    fasta_generation_errs = generate_fasta(
        merged_df[~merged_df["wa_no"].isin(failed_samples)], logger
    )
    failed_samples.extend(list(fasta_generation_errs.keys()))
    pha4ge_metadata_df = get_pha4ge_metadata(
        merged_df[~merged_df["wa_no"].isin(failed_samples)],
        logger
    )#.set_index('specimen_collector_sample_id', drop=True)
    drop_reasons = pha4ge_metadata_df[
        pha4ge_metadata_df['purpose_of_sequencing'] == 'DROP'
    ]
    gisaid_metadata_df = (
        get_gisaid_metadata(pha4ge_metadata_df, logger)
        .droplevel(1, axis=1)
        .set_index("submitter")
    )
    # Next step is to generate genbank_metadata_df
    genbank_metadata_df = (
        get_genbank_metadata(pha4ge_metadata_df, logger)
    )

    # quick test; delete later
    # blank_df = pd.DataFrame({})
    # gisaid_metadata_df, genbank_metadata_df = blank_df.copy(), blank_df.copy()
    # end suspect block

    biosample_metadata_df = get_biosample_metadata(pha4ge_metadata_df, logger)
    genbank_metadata_df = get_genbank_metadata(pha4ge_metadata_df, logger)

    pha4ge_outpath = os.path.join(OUTDIR, "pha4ge_metadata.csv")
    pha4ge_outpath, gisaid_outpath, biosample_outpath, genbank_outpath = (
        os.path.join(OUTDIR, filename) for filename in
        ('pha4ge_metadata.csv', 'gisaid_metadata.csv', 'biosample_metadata.csv', 'genbank_metadata.csv')
    )

    # A brief block to provide any final formatting to metadata files
    # pha4ge_metadata_df.set_index('specimen_collector_sample_id', drop=True, inplace=True)
    # biosample_metadata_df.set_index('sample_name', drop=True, inplace=True)
    # genbank_metadata_df.set_index('sequence_ID', drop=True, inplace=True)

    for metadata_df, new_index, metadata_table_name, outpath in zip(
        (pha4ge_metadata_df, gisaid_metadata_df, biosample_metadata_df, genbank_metadata_df),
        ('specimen_collector_sample_id', 'submitter', 'sample_name', 'sequence_ID'),
        ("PHA4GE", "GISAID", "BioSample", "GenBank"),
        (pha4ge_outpath, gisaid_outpath, biosample_outpath, genbank_outpath),
    ):
        if metadata_table_name != "GISAID":
            metadata_df.set_index(new_index, drop=True, inplace=True)
        metadata_df.to_csv(outpath)
        logger.info(f"{metadata_table_name} metadata file written to {outpath}")

    print("Done", end="\n\n")


if __name__ == "__main__":
    main()
