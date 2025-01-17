#!/usr/bin/env bash


#SBATCH --cluster=gpu
#SBATCH --gres=gpu:1
#SBATCH --partition=titanx
#SBATCH --job-name=bert_ws
#SBATCH --output=bert_ws.out
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --time=6-00:00:00
#SBATCH --qos=long
#SBATCH --mem=16g
#SBATCH --mail-user=saz31@pitt.edu #send email to this address if ...
#SBATCH --mail-type=END,FAIL # ... job ends or fails


# Load modules
module restore

# Run the job
srun python ../../../model/train.py -ngpus 1 -bsize 10 -fw transformer -out bertbase_ws -layer_drop 0.0 -op adagrad -lr 0.01 --mode wikisplit -nh 4 -nhl 4 -nel 4 -ndl 4 -lc True -eval_freq 0 --fetch_mode tf_example_dataset --subword_vocab_size 0 --tie_embedding enc_dec --bert_mode bert_token:bertbase --number_samples 4096


# python train.py -ngpus 1 -bsize 10 -fw transformer -out bertbase_ws -layer_drop 0.0 -op adagrad -lr 0.01 --mode wikisplit -nh 4 -nhl 4 -nel 4 -ndl 4 -lc True -eval_freq 0 --fetch_mode tf_example_dataset --subword_vocab_size 0 --tie_embedding enc_dec --bert_mode bert_token:bertbase --number_samples 4096 &
