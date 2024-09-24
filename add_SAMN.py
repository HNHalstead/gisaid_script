import argparse
import os
import glob
import pandas as pd


# Set up argument parser
parser = argparse.ArgumentParser(description="Add NCBI SAMN accession to the results file generated by gisaid.py script. this will be used for the Dashboard upload.")
parser.add_argument(
    "-i", "--indir",
    help="Path to directory containing input tables (default is the current directory)",
    type=str,
    dest="input_directory",
    default=os.getcwd()
)
parser.add_argument(
    "-r", "--results",
    help="Name of the results file (default is a file with 'results' in the name in the input directory)",
    type=str,
    dest="results_file",
    default=None
)
parser.add_argument(
    "-n", "--ncbi",
    help="Name of the NCBI file (default is a file with 'NCBI' in the name in the input directory)",
    type=str,
    dest="ncbi_pattern",
    default='*BioSample*.csv'
)
parser.add_argument(
    "-o", "--outdir",
    help="Path to directory to which outputs should be written (default is the current directory)",
    type=str,
    dest="output_directory",
    default=os.getcwd()
)
# Parse the arguments
args = parser.parse_args()

#function to find files matching pattern and merge multiple accession files 
def find_files(directory, pattern):
    files = glob.glob(os.path.join(directory, pattern))
    if files:
        return files  # Return all matching files
    else:
        raise FileNotFoundError(f"No files matching pattern '{pattern}' found in directory '{directory}'.")
    
# Use the find_file function to locate specific files based on patterns
input_directory = args.input_directory
if args.results_file:
    results_path = args.results_file
else:
    results_files = find_files(input_directory, '*results*.csv')
    if len(results_files) == 1:
        results_path = results_files[0]  # Take the first and only file
    else:
        raise FileNotFoundError(f"Expected exactly one results file, found: {len(results_files)}")
ncbi_files= find_files(input_directory, args.ncbi_pattern)
output_directory = args.output_directory

# Print paths for debugging
print(f"Results file: {results_path}")
print(f"NCBI files: {ncbi_files}")
print(f"Output directory: {output_directory}")

# Define a function to extract the common identifier
def extract_identifier_from_virus_name(virus_name):
    if isinstance(virus_name, str):  # Check if the value is a string
        parts = virus_name.split('/')
        if len(parts) >= 3:
            return parts[2]
    return None
   

def extract_identifier_from_isolate(isolate):
    # Assuming the format SARS-CoV-2/Human/USA/WA-PHL-034983/2024
    if isinstance(isolate, str):  # Check if the value is a string
        parts = isolate.split('/')
        if len(parts) >= 4:
            return parts[3]
    return None
   

# Process the data
def main():
    # Read in the NCBI accessions csv, merge if needed
    df_ncbi = pd.concat([pd.read_csv(file) for file in ncbi_files])

    #Read in results file from gisaid script 
    df_results = pd.read_csv(results_path)

     # Extract the common identifier
    df_results['common_id'] = df_results['Virus name'].apply(extract_identifier_from_virus_name)
    df_ncbi['common_id'] = df_ncbi['Isolate'].apply(extract_identifier_from_isolate)

    # Perform the merge and populate the NCBI Accession column with the Accession value
    df_merged = pd.merge(df_results, df_ncbi[['common_id', 'Accession']], on='common_id', how='left')
    df_results['NCBI Accession'] = df_merged['Accession']
    df_results.drop(columns=['common_id'], inplace=True)

    # Drop the 'SPUID' column if it's not needed
    #results_with_ncbi.drop(columns=['SPUID'], inplace=True)

    # Save the updated dataframe
    output_path = os.path.join(output_directory, 'results_with_ncbi.csv')
    df_results.to_csv(output_path, index=False)
    print(f"Updated results saved to {output_path}")

if __name__ == "__main__":
    main()

