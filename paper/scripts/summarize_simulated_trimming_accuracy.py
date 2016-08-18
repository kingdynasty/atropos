
#!/usr/bin/env python
# Summarizes trimming accuracy for simulated reads.

from argparse import ArgumentParser
from atropos import xopen
import csv
import pylev
from common import *

def aln_iterator(i):
    for line in i:
        if line[0] in ('@','#'):
            continue
        assert line[0] == '>'
        chrm, name, pos, strand = line[1:].rstrip().split('\t')
        if name.endswith('-1'):
            name = name[:-2]
        mate = name[-1]
        name = name[:-2]
        ref = next(i).rstrip()
        actual = next(i).rstrip()
        yield (name, mate, chrm, pos, strand, ref, actual)

def fq_iterator(i, mate):
    for read in zip(*[i] * 4):
        name = read[0].rstrip()[1:]
        mate = name[-1]
        name = name[:-2]
        yield (name, mate, read[1].rstrip())

def summarize_accuracy(aln_iter, read_iter, w, read_length, adapters, progress=True):
    adapter_lengths = [len(adapters[i]) for i in (0,1)]
    
    #if read_id != 'chr1-199374': continue
    debug = False
    
    def summarize_alignment(a):
        ref_seq = a[5]
        if debug: print(ref_seq)
        ref_len = len(ref_seq)
        if debug: print(ref_len)
        read_seq = a[6]
        if debug: print(read_seq)
        read_len = len(read_seq)
        if debug: print(read_len)
        
        ref_ins = [i for i in range(len(ref_seq)) if ref_seq[i] == '-']
        expected_read = "".join(b for b in read_seq[0:ref_len] if b != '-')
        expected_read_len = len(expected_read)
        ref_del = ref_len - expected_read_len
        
        has_adapter = ref_len < read_len
        adapter_seq = []
        adapter_len = adapter_ins = adapter_del = polyA = 0
        if debug: print(has_adapter)
        
        if has_adapter:
            for b in read_seq[ref_len:]:
                if adapter_len >= adapter_lengths[i] and b == 'A':
                    polyA += 1
                else:
                    if b == '-':
                        adapter_del += 1
                    else:
                        adapter_seq.append(b)
                        adapter_len += 1
            
            adapter_ref_len = adapter_len + adapter_del
            if adapter_ref_len > adapter_lengths[i]:
                adapter_ins = adapter_ref_len - adapter_lengths[i]
        
        edit_dist = pylev.levenshtein(adapters[i][:adapter_len], "".join(adapter_seq))
        return [expected_read, (int(has_adapter), adapter_len, edit_dist, adapter_ins, adapter_del, polyA)]
    
    if progress:
        import tqdm
        read_iter = tqdm.tqdm(read_iter)
    
    cache = {}
    overtrimmed = 0
    undertrimmed = 0
    raw_trimmed_mismatch = 0
    for num_reads, reads in enumerate(read_iter, 1):
        read_id = reads[0][0]
        aln = None
        
        assert read_id == reads[1][0], "Read IDs differ - {} != {}".format(read_id, reads[1][0])
        assert int(reads[0][1]) == 1 and int(reads[1][1]) == 2, "Mate identifiers are incorrect for {}".format(read_id)
        if read_id not in cache:
            for aln in aln_iter:
                if read_id == aln[0][0]:
                    break
                else:
                    cache[aln[0][0]] = aln
        else:
            aln = cache.pop(read_id)
        
        if debug: print(reads)
        if debug: print(aln)
        
        if aln is None:
            raise Exception("No alignment for read {}".format(read_id))
        
        for i in (0,1):
            expected_read, adapter_info = summarize_alignment(aln[i])
            expected_read_len = len(expected_read)
            
            r = reads[i]
            trimmed_len = len(r[2])
            
            if debug: print(trimmed_len)
            if debug: print(r[2])
            if debug: print(expected_read)
            
            status = 'OK'
            common_len = min(trimmed_len, expected_read_len)
            if expected_read_len > trimmed_len:
                overtrimmed += expected_read_len - trimmed_len
                status = 'OVERTRIMMED'
            elif expected_read_len < trimmed_len:
                undertrimmed += trimmed_len - expected_read_len
                status = 'UNDERTRIMMED'
            if r[2][:common_len] != expected_read[:common_len]:
                raw_trimmed_mismatch += 1
                status = 'MISMATCH'
            
            w.writerow((read_id, i+1, expected_read_len, trimmed_len, status) + adapter_info)

    # all remaining alignments represent reads that were discarded
    
    def handle_discarded(aln):
        read_id = aln[0][0]
        for i in (0, 1):
            expected_read, adapter_info = summarize_alignment(aln[i])
            w.writerow((read_id, i+1, len(expected_read), '', 'DISCARDED') + adapter_info)
    
    num_discarded = len(cache)
    for aln in cache.values():
        handle_discarded(aln)
    for aln in aln_iter:
        handle_discarded(aln)
        num_discarded += 1
    
    print("{} retained reads".format(num_reads))
    print("{} mismatch reads".format(raw_trimmed_mismatch))
    print("{} discarded reads".format(num_discarded))
    print("{} total reads".format(num_reads + num_discarded))
    print("{} overtrimmed bases".format(overtrimmed))
    print("{} undertrimmed bases".format(undertrimmed))

def main():
    parser = ArgumentParser()
    parser.add_argument('-a1', '--aln1', help=".aln file associated with simulated read1")
    parser.add_argument('-a2', '--aln2', help=".aln file associated with simulated read2")
    parser.add_argument('-r1', '--reads1', help="trimmed fastq file read1")
    parser.add_argument('-r2', '--reads2', help="trimmed fastq file read1")
    parser.add_argument('-l', '--read-length', type=int, default=125)
    parser.add_argument('-o', '--output', default='-')
    parser.add_argument("--adapters", nargs=2, default=DEFAULT_ADAPTERS)
    args = parser.parse_args()
    
    with open(args.aln1, 'rt') as a1, open(args.aln2, 'rt') as a2:
        aln_pair_iterator = zip(aln_iterator(a1), aln_iterator(a2))
        
        with xopen.xopen(args.reads1, 'rt') as r1, xopen.xopen(args.reads2, 'rt') as r2:
            read_pair_iterator = zip(fq_iterator(r1, 1), fq_iterator(r2, 2))
            
            with fileoutput(args.output) as o:
                w = csv.writer(o, delimiter="\t")
                w.writerow((
                    'read_id','mate','expected_len','actual_len','status','has_adapter',
                    'adapter_len','adapter_edit_dist','adapter_ins','adapter_del','polyA'))
                summarize_accuracy(aln_pair_iterator, read_pair_iterator, w, args.read_length, args.adapters)

if __name__ == "__main__":
    main()
