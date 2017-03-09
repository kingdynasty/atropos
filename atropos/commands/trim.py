"""Implementation of the 'trim' command.
"""
from collections import defaultdict
import logging
import sys
import time
import textwrap

import atropos.trim
from atropos.commands.stats import *
from atropos.io.xopen import STDOUT
from atropos.report.text import print_report
from atropos.util import run_interruptible, run_interruptible_with_result

def trim(options, parser):
    reader, pipeline, formatters, writers = create_trim_params(
        options, parser, options.default_outfile)
    num_adapters = sum(len(a) for a in pipeline.modifiers.get_adapters())
    
    logger = logging.getLogger()
    logger.info("Trimming %s adapter%s with at most %.1f%% errors in %s mode ...",
        num_adapters, 's' if num_adapters > 1 else '', options.error_rate * 100,
        { False: 'single-end', 'first': 'paired-end legacy', 'both': 'paired-end' }[options.paired])
    if options.paired == 'first' and (len(pipeline.modifiers.get_modifiers(read=2)) > 0 or options.quality_cutoff):
        logger.warning('\n'.join(textwrap.wrap('WARNING: Requested read '
            'modifications are applied only to the first '
            'read since backwards compatibility mode is enabled. '
            'To modify both reads, also use any of the -A/-B/-G/-U options. '
            'Use a dummy adapter sequence when necessary: -A XXX')))
    
    start_wallclock_time = time.time()
    start_cpu_time = time.clock()
    
    if options.threads is None:
        # Run single-threaded version
        from atropos.trim import run_serial
        rc, summary, details = run_serial(reader, pipeline, formatters, writers)
    else:
        # Run multiprocessing version
        from atropos.trim import run_parallel
        rc, summary, details = run_parallel(
            reader, pipeline, formatters, writers, options.threads,
            options.process_timeout, options.preserve_order,
            options.read_queue_size, options.result_queue_size,
            options.writer_process, options.compression)
    
    reader.close()
    
    if rc != 0:
        return (rc, None, details)
    
    stop_wallclock_time = time.time()
    stop_cpu_time = time.clock()
    adapter_stats = print_report(
        options,
        stop_wallclock_time - start_wallclock_time,
        stop_cpu_time - start_cpu_time,
        summary,
        pipeline.modifiers.get_trimmer_classes())
    
    details['stats'] = adapter_stats
    return (rc, None, details)

def create_trim_params(options, parser, default_outfile):
    from atropos.adapters import AdapterParser, BACK
    from atropos.modifiers import (
        Modifiers, AdapterCutter, InsertAdapterCutter, UnconditionalCutter,
        NextseqQualityTrimmer, QualityTrimmer, NonDirectionalBisulfiteTrimmer,
        RRBSTrimmer, SwiftBisulfiteTrimmer, MinCutter, NEndTrimmer,
        LengthTagModifier, SuffixRemover, PrefixSuffixAdder, DoubleEncoder,
        ZeroCapper, PrimerTrimmer, MergeOverlapping, OverwriteRead)
    from atropos.filters import (
        Filters, FilterFactory, TooShortReadFilter, TooLongReadFilter,
        NContentFilter, TrimmedFilter, UntrimmedFilter, NoFilter,
        MergedReadFilter)
    from atropos.trim import Pipeline, PipelineWithStats
    from atropos.seqio import Formatters, RestFormatter, InfoFormatter, WildcardFormatter, Writers
    from atropos.util import RandomMatchProbability
    
    reader, input_names, qualities, has_qual_file = create_reader(options, parser)
    
    if options.adapter_max_rmp or options.aligner == 'insert':
        match_probability = RandomMatchProbability()
    
    # Create Adapters
    
    has_adapters1 = options.adapters or options.anywhere or options.front
    has_adapters2 = options.adapters2 or options.anywhere2 or options.front2
    
    adapters1 = adapters2 = []
    if has_adapters1 or has_adapters2:
        adapter_cache = load_known_adapters(options)
        parser_args = dict(
            colorspace=options.colorspace,
            max_error_rate=options.error_rate,
            min_overlap=options.overlap,
            read_wildcards=options.match_read_wildcards,
            adapter_wildcards=options.match_adapter_wildcards,
            indels=options.indels, indel_cost=options.indel_cost,
            cache=adapter_cache
        )
        if options.adapter_max_rmp:
            parser_args['match_probability'] = match_probability
            parser_args['max_rmp'] = options.adapter_max_rmp
        adapter_parser = AdapterParser(**parser_args)
        
        try:
            if has_adapters1:
                adapters1 = adapter_parser.parse_multi(
                    options.adapters, options.anywhere, options.front)
            if has_adapters2:
                adapters2 = adapter_parser.parse_multi(
                    options.adapters2, options.anywhere2, options.front2)
        except IOError as e:
            if e.errno == errno.ENOENT:
                parser.error(e)
            raise
        except ValueError as e:
            parser.error(e)
        
        if options.cache_adapters:
            adapter_cache.save()
    
    # Create Modifiers
    
    # TODO: can this be replaced with an argparse required group?
    if not adapters1 and not adapters2 and not options.quality_cutoff and \
            options.nextseq_trim is None and \
            options.cut == [] and options.cut2 == [] and \
            options.cut_min == [] and options.cut_min2 == [] and \
            (options.minimum_length is None or options.minimum_length <= 0) and \
            options.maximum_length == sys.maxsize and \
            not has_qual_file and options.max_n is None and not options.trim_n \
            and (not options.paired or options.overwrite_low_quality is None):
        parser.error("You need to provide at least one adapter sequence.")
    
    if options.aligner == 'insert':
        if not adapters1 or len(adapters1) != 1 or adapters1[0].where != BACK or \
                not adapters2 or len(adapters2) != 1 or adapters2[0].where != BACK:
            parser.error("Insert aligner requires a single 3' adapter for each read")
    
    if options.debug:
        for adapter in adapters1 + adapters2:
            adapter.enable_debug()
    
    modifiers = Modifiers(options.paired)
            
    for op in options.op_order:
        if op == 'W' and options.overwrite_low_quality:
            lowq, highq, window = options.overwrite_low_quality
            modifiers.add_modifier(OverwriteRead,
                worse_read_min_quality=lowq, better_read_min_quality=highq,
                window_size=window, base=options.quality_base)
            
        elif op == 'A' and (adapters1 or adapters2):
            # TODO: generalize this using some kind of factory class
            if options.aligner == 'insert':
                # Use different base probabilities if we're trimming bisulfite data.
                # TODO: this doesn't seem to help things, so commenting it out for now
                #base_probs = dict(p1=0.33, p2=0.67) if options.bisulfite else dict(p1=0.25, p2=0.75)
                modifiers.add_modifier(InsertAdapterCutter,
                    adapter1=adapters1[0], adapter2=adapters2[0], action=options.action,
                    mismatch_action=options.correct_mismatches,
                    max_insert_mismatch_frac=options.insert_match_error_rate,
                    max_adapter_mismatch_frac=options.insert_match_adapter_error_rate,
                    match_probability=match_probability,
                    insert_max_rmp=options.insert_max_rmp)
            else:
                a1_args = a2_args = None
                if adapters1:
                    a1_args = dict(adapters=adapters1, times=options.times, action=options.action)
                if adapters2:
                    a2_args = dict(adapters=adapters2, times=options.times, action=options.action)
                modifiers.add_modifier_pair(AdapterCutter, a1_args, a2_args)
        elif op == 'C' and (options.cut or options.cut2):
            modifiers.add_modifier_pair(UnconditionalCutter,
                dict(lengths=options.cut),
                dict(lengths=options.cut2)
            )
        elif op == 'G' and (options.nextseq_trim is not None):
            modifiers.add_modifier(NextseqQualityTrimmer,
                read=1, cutoff=options.nextseq_trim, base=options.quality_base)
        elif op == 'Q' and options.quality_cutoff:
            modifiers.add_modifier(QualityTrimmer,
                cutoff_front=options.quality_cutoff[0],
                cutoff_back=options.quality_cutoff[1],
                base=options.quality_base)
    
    if options.bisulfite:
        if isinstance(options.bisulfite, str):
            if "non-directional" in options.bisulfite:
                modifiers.add_modifier(NonDirectionalBisulfiteTrimmer,
                    rrbs=options.bisulfite=="non-directional-rrbs")
            elif options.bisulfite == "rrbs":
                modifiers.add_modifier(RRBSTrimmer)
            elif options.bisulfite in ("epignome", "truseq"):
                # Trimming leads to worse results
                #modifiers.add_modifier(TruSeqBisulfiteTrimmer)
                pass
            elif options.bisulfite == "swift":
                modifiers.add_modifier(SwiftBisulfiteTrimmer)
        else:
            if options.bisulfite[0]:
                modifiers.add_modifier(MinCutter, read=1, **(options.bisulfite[0]))
            if len(options.bisulfite) > 1 and options.bisulfite[1]:
                modifiers.add_modifier(MinCutter, read=2, **(options.bisulfite[1]))
    
    if options.trim_n:
        modifiers.add_modifier(NEndTrimmer)
    
    if options.cut_min or options.cut_min2:
        modifiers.add_modifier_pair(MinCutter,
            dict(lengths=options.cut_min),
            dict(lengths=options.cut_min2)
        )
    
    if options.length_tag:
        modifiers.add_modifier(LengthTagModifier, length_tag=options.length_tag)
    
    if options.strip_suffix:
        modifiers.add_modifier(SuffixRemover, suffixes=options.strip_suffix)
    
    if options.prefix or options.suffix:
        modifiers.add_modifier(PrefixSuffixAdder, prefix=options.prefix, suffix=options.suffix)
    
    if options.double_encode:
        modifiers.add_modifier(DoubleEncoder)
    
    if options.zero_cap and qualities:
        modifiers.add_modifier(ZeroCapper, quality_base=options.quality_base)
    
    if options.trim_primer:
        modifiers.add_modifier(PrimerTrimmer)
    
    if options.merge_overlapping:
        modifiers.add_modifier(MergeOverlapping,
            min_overlap=options.merge_min_overlap,
            error_rate=options.merge_error_rate,
            mismatch_action=options.correct_mismatches)
    
    # Create Filters and Formatters
    
    min_affected = 2 if options.pair_filter == 'both' else 1
    filters = Filters(FilterFactory(options.paired, min_affected))
    
    output1 = output2 = None
    interleaved = False
    if options.interleaved_output:
        output1 = options.interleaved_output
        interleaved = True
    else:
        output1 = options.output
        output2 = options.paired_output
    
    seq_formatter_args = dict(
        qualities=qualities,
        colorspace=options.colorspace,
        interleaved=interleaved
    )
    formatters = Formatters(output1, seq_formatter_args)
    force_create = []
        
    if options.merge_overlapping:
        filters.add_filter(MergedReadFilter)
        if options.merged_output:
            formatters.add_seq_formatter(MergedReadFilter, options.merged_output)
        
    if options.minimum_length is not None and options.minimum_length > 0:
        filters.add_filter(TooShortReadFilter, options.minimum_length)
        if options.too_short_output:
            formatters.add_seq_formatter(TooShortReadFilter,
                options.too_short_output, options.too_short_paired_output)

    if options.maximum_length < sys.maxsize:
        filters.add_filter(TooLongReadFilter, options.maximum_length)
        if options.too_long_output is not None:
            formatters.add_seq_formatter(TooLongReadFilter,
                options.too_long_output, options.too_long_paired_output)

    if options.max_n is not None:
        filters.add_filter(NContentFilter, options.max_n)

    if options.discard_trimmed:
        filters.add_filter(TrimmedFilter)

    if not formatters.multiplexed:
        if output1 is not None:
            formatters.add_seq_formatter(NoFilter, output1, output2)
            if output1 != STDOUT and options.writer_process:
                force_create.append(output1)
                if output2 is not None:
                    force_create.append(output2)
        elif not (options.discard_trimmed and options.untrimmed_output):
            formatters.add_seq_formatter(NoFilter, default_outfile)
            if default_outfile != STDOUT and options.writer_process:
                force_create.append(default_outfile)
    
    if options.discard_untrimmed or options.untrimmed_output:
        filters.add_filter(UntrimmedFilter)

    if not options.discard_untrimmed:
        if formatters.multiplexed:
            untrimmed = options.untrimmed_output or output1.format(name='unknown')
            formatters.add_seq_formatter(UntrimmedFilter, untrimmed)
            formatters.add_seq_formatter(NoFilter, untrimmed)
        elif options.untrimmed_output:
            formatters.add_seq_formatter(UntrimmedFilter,
                options.untrimmed_output, options.untrimmed_paired_output)

    if options.rest_file:
        formatters.add_info_formatter(RestFormatter(options.rest_file))
    if options.info_file:
        formatters.add_info_formatter(InfoFormatter(options.info_file))
    if options.wildcard_file:
        formatters.add_info_formatter(WildcardFormatter(options.wildcard_file))
    
    writers = Writers(force_create)
    
    if options.stats:
        read_stats = ReadStatistics(
            options.stats, options.paired, qualities=qualities,
            tile_key_regexp=options.tile_key_regexp)
        pipeline = PipelineWithStats(modifiers, filters, read_stats)
    else:
        pipeline = Pipeline(modifiers, filters)
    
    return (reader, pipeline, formatters, writers)

class Pipeline(object):
    def __init__(self, modifiers, filters):
        self.modifiers = modifiers
        self.filters = filters
        self.total_bp1 = 0
        self.total_bp2 = 0
    
    def __call__(self, record):
        reads, bp = self.modifiers.modify(record)
        self.total_bp1 += bp[0]
        self.total_bp2 += bp[1]
        dest = self.filters.filter(*reads)
        return (dest, reads)
    
    def summarize_adapters(self):
        adapters = self.modifiers.get_adapters()
        summary = [{}, {}]
        if adapters[0]:
            summary[0] = collect_adapter_statistics(adapters[0])
        if adapters[1]:
            summary[1] = collect_adapter_statistics(adapters[1])
        return summary

class PipelineWithStats(Pipeline):
    def __init__(self, modifiers, filters, read_stats):
        super().__init__(modifiers, filters)
        self.read_stats = read_stats
    
    def __call__(self, record):
        self.read_stats.pre_trim(record)
        dest, reads = super().__call__(record)
        self.read_stats.post_trim(dest, reads)
        return (dest, reads)

def run_serial(reader, pipeline, formatters, writers):
    def _run():
        n = 0
        for batch_size, batch in reader:
            n += batch_size
            result = defaultdict(lambda: [])
            for record in batch:
                dest, reads = pipeline(record)
                formatters.format(result, dest, *reads)
            result = dict((path, "".join(strings))
                for path, strings in result.items())
            writers.write_result(result)
        return n
    
    try:
        rc, n = run_interruptible_with_result(_run)
    finally:
        reader.close()
        writers.close()
    
    report = None
    if rc == 0:
        report = Summary(
            collect_process_statistics(
                n, pipeline.total_bp1, pipeline.total_bp2, pipeline.modifiers,
                pipeline.filters, formatters),
            pipeline.summarize_adapters(),
            pipeline.modifiers.get_trimmer_classes()
        ).finish()
    
    details = dict(mode='serial', threads=1)
    return (rc, report, details)

# Parallel implementation of run_atropos. Works as follows:
#
# 1. Main thread creates N worker processes (where N is the number of threads to be allocated) and
# (optionally) one writer process.
# 2. Main thread loads batches of reads (or read pairs) from input file(s) and adds them to a queue
# (the input queue).
# 3. Worker processes take batches from the input queue, process them as atropos normally does,
# and either add the results to the result queue (if using a writer process) or write the results
# to disk. Each result is a dict mapping output file names to strings, where each string is a
# concatenation of reads (with appropriate line endings) to be written. A parameter also controls
# whether data compression is done by the workers or the writer.
# 4. If using a writer process, it takes results from the result queue and writes each string to
# its corresponding file.
# 5. When the main process finishes loading reads from the input file(s), it sends a signal to the
# worker processes that they should complete when the input queue is empty. It also singals the
# writer process how many total batches to expect, and the writer process exits after it has
# processed that many batches.
# 6. When a worker process completes, it adds a summary of its activity to the summary queue.
# 7. The main process reads summaries from the summary queue and merges them to create the complete
# summary, which is used to generate the report.
#
# There are several possible points of failure:
#
# 1. The main process may exit due to an unexpected error, or becuase the user forces it to exit
# (Ctrl-C). In this case, an attempt is made to cancel all processes before exiting.
# 2. A worker or writer process may exit due to an unknown error. To handle this, the main process
# checks that each process is alive whenver it times out writing to the input queue, and again when
# waiting for worker summaries. If a process has died, the program exits with an error since some data
# might have gotten lost.
# 3. More commonly, process will time out blocking on reading from or writing to a queue. Size
# limits are used (optionally) for the input and result queues to prevent using lots of memory. When
# few threads are allocated, it is most likely that the main and writer processes will block, whereas
# with many threads allocated the workers are most likely to block. Also, e.g. in a cluster
# environment, I/O latency may cause a "backup" resulting in frequent blocking of the main and workers
# processes. Finally, also e.g. in a cluster environment, processes may suspended for periods of time.
# Use of a hard timeout period, after which processes are forced to exit, is thus undesirable.
# Instead, parameters are provided for the user to tune the batch size and max queue sizes to their
# particular environment. Additionally, a "soft" timeout is used, after which log messages are
# escallated from DEBUG to ERROR level. The user can then make the decision of whether or not to kill
# the program.

import logging
from multiprocessing import Queue
from atropos.multicore import *
from atropos.compression import get_compressor, can_use_system_compression

class TrimWorkerProcess(ResultHandlerWorkerProcess):
    """
    
    Args:
        index: A unique ID for the process
        pipeline: The trimming pipeline
        formatters: A Formatters object
        input_queue: queue with batches of records to process
        result_handler: A ResultHandler object
        summary_queue: queue where summary information is written
        timeout: time to wait upon queue full/empty
    """
    def __init__(self, index, input_queue, summary_queue, timeout,
                 result_handler, pipeline, formatters):
        super().__init__(index, input_queue, summary_queue, timeout, result_handler)
        self.pipeline = pipeline
        self.formatters = formatters
    
    def _handle_record(self, record, result):
        dest, reads = self.pipeline(record)
        self.formatters.format(result, dest, *reads)
    
    def _get_summary(self, error=False):
        if error:
            process_stats = adapter_stats = None
        else:
            process_stats = collect_process_statistics(
                self.processed_reads, self.pipeline.total_bp1, self.pipeline.total_bp2,
                self.pipeline.modifiers, self.pipeline.filters, self.formatters)
            adapter_stats = self.pipeline.summarize_adapters()
        return (self.index, self.seen_batches, process_stats, adapter_stats)

class WorkerResultHandler(ResultHandlerWrapper):
    """Wraps a ResultHandler and compresses results prior to writing.
    """
    def write_result(self, batch_num, result):
        """
        Given a dict mapping files to lists of strings,
        join the strings and compress them (if necessary)
        and then return the property formatted result
        dict.
        """
        self.handler.write_result(
            batch_num, dict(
                self.prepare_file(*item)
                for item in result.items()))
    
    def prepare_file(self, path, strings):
        return (path, "".join(strings))

class CompressingWorkerResultHandler(WorkerResultHandler):
    """
    Wraps a ResultHandler and compresses results prior
    to writing.
    """
    def start(self, worker):
        super().start(worker)
        self.file_compressors = {}
    
    def prepare_file(self, path, strings):
        compressor = self.get_compressor(path)
        if compressor:
            return ((path, 'wb'), compressor.compress(b''.join(
                s.encode() for s in strings)))
        else:
            return ((path, 'wt'), "".join(strings))
    
    def get_compressor(self, filename):
        if filename not in self.file_compressors:
            self.file_compressors[filename] = get_compressor(filename)
        return self.file_compressors[filename]

class WriterResultHandler(ResultHandler):
    """
    ResultHandler that writes results to disk.
    """
    def __init__(self, writers, compressed=False, use_suffix=False):
        self.writers = writers
        self.compressed = compressed
        self.use_suffix = use_suffix
    
    def start(self, worker):
        if self.use_suffix:
            self.writers.suffix = ".{}".format(worker.index)
    
    def write_result(self, batch_num, result):
        self.writers.write_result(result, self.compressed)
    
    def finish(self, total_batches=None):
        self.writers.close()

class OrderPreservingWriterResultHandler(WriterResultHandler):
    """
    Writer thread that is less time/memory efficient, but is
    guaranteed to preserve the original order of records.
    """
    def start(self, worker):
        super().__init__(worker)
        self.pending = PendingQueue()
        self.cur_batch = 1
    
    def write_result(self, batch_num, result):
        if batch_num == self.cur_batch:
            self.writers.write_result(result, self.compressed)
            self.cur_batch += 1
            self.consume_pending()
        else:
            self.pending.push(batch_num, result)
    
    def finish(self, total_batches):
        if total_batches is not None:
            self.consume_pending()
            if self.cur_batch != total_batches:
                raise Exception("OrderPreservingWriterResultHandler finishing without having seen "
                                "{} batches".format(total_batches))
        self.writers.close()
    
    def consume_pending(self):
        while (not self.pending.empty) and (self.cur_batch == pending.min_priority):
            self.writers.write_result(pending.pop(), self.compressed)
            self.cur_batch += 1

def run_parallel(reader, pipeline, formatters, writers, threads=2, timeout=30,
                 preserve_order=False, input_queue_size=0, result_queue_size=0,
                 use_writer_process=True, compression=None):
    """
    Execute atropos in parallel mode.
    
    reader 				:: iterator over batches of reads (most likely a BatchIterator)
    pipeline 			::
    formatters          ::
    writers				::
    threads				:: number of worker threads to use; additional threads are used
                        for the main proccess and the writer process (if requested).
    timeout				:: number of seconds after which waiting processes escalate their
                        messages from DEBUG to ERROR.
    preserve_order 		:: whether to preserve the input order of reads when writing
                        (only valid when `use_writer_process=True`)
    input_queue_size 	:: max number of items that can be in the input queue, or 0 for
                        no limit (be warned that this could explode memory usage)
    result_queue_size	:: max number of items that can be in the result queue, or 0 for
                        no limit (be warned that this could explode memory usage)
    use_writer_process	:: if True, a separate thread will be used to write results to
                        disk. Otherwise, each worker thread will write its results to
                        an output file with a '.N' extension, where N is the thread index.
                        This is useful in cases where the I/O is the main bottleneck.
    compression	        If "writer", the writer process perform data compression, otherwise
                        the worker processes performs compression.
    """
    logging.getLogger().debug(
        "Starting atropos in parallel mode with threads={}, timeout={}".format(threads, timeout))
    
    if threads < 2:
        raise ValueError("'threads' must be >= 2")
    
    # Reserve a thread for the writer process if it will be doing the compression and if one is available.
    if compression is None:
        compression = "writer" if use_writer_process and can_use_system_compression() else "worker"
    if compression == "writer" and threads > 2:
        threads -= 1
    
    timeout = max(timeout, RETRY_INTERVAL)
    
    # Queue by which batches of reads are sent to worker processes
    input_queue = Queue(input_queue_size)
    # Queue by which results are sent from the worker processes to the writer process
    result_queue = Queue(result_queue_size)
    # Queue for processes to send summary information back to main process
    summary_queue = Queue(threads)
    # Aggregate summary
    summary = Summary(trimmer_classes=pipeline.modifiers.get_trimmer_classes())
    
    if use_writer_process:
        worker_result_handler = QueueResultHandler(result_queue)
        if compression == "writer":
            worker_result_handler = WorkerResultHandler(worker_result_handler)
        else:
            worker_result_handler = CompressingWorkerResultHandler(worker_result_handler)
        
        # Shared variable for communicating with writer thread
        writer_control = Control(CONTROL_ACTIVE)
        # result handler
        if preserve_order:
            writer_result_handler = OrderPreservingWriterResultHandler(
                writers, compressed=compression == "worker")
        else:
            writer_result_handler = WriterResultHandler(
                writers, compressed=compression == "worker")
        # writer process
        writer_process = ResultProcess(writer_result_handler, result_queue, writer_control, timeout)
        writer_process.start()
    else:
        worker_result_handler = WorkerResultHandler(WriterResultHandler(writers, use_suffix=True))
    
    # Start worker processes, reserve a thread for the reader process,
    # which we will get back after it completes
    worker_args = (input_queue, summary_queue, timeout, worker_result_handler, pipeline, formatters)
    worker_processes = launch_workers(threads - 1, TrimWorkerProcess, worker_args)
    
    def ensure_alive():
        ensure_processes(worker_processes)
        if (use_writer_process and not (
                writer_process.is_alive() and
                writer_control.check_value(CONTROL_ACTIVE))):
            raise Exception("Writer process exited")
    
    def _run(worker_processes):
        # Add batches of reads to the input queue. Provide a timeout callback
        # to check that subprocesses are alive.
        num_batches = enqueue_all(
            enumerate(reader, 1), input_queue, timeout, ensure_alive)
        logging.getLogger().debug(
            "Main loop complete; saw {} batches".format(num_batches))
        
        # Tell the worker processes no more input is coming
        enqueue_all((None,) * threads, input_queue, timeout, ensure_alive)
        
        # Tell the writer thread the max number of batches to expect
        if use_writer_process:
            writer_control.set_value(num_batches)
        
        # Now that the reader process is done, it essentially
        # frees up another thread to use for a worker
        worker_processes.extend(
            launch_workers(1, TrimWorkerProcess, worker_args, offset=threads-1))
        
        # Wait for all summaries to be available on queue
        def summary_timeout_callback():
            try:
                ensure_processes(worker_processes,
                    "Workers are still alive and haven't returned summaries: {}",
                    alive=False)
            except Exception as e:
                logging.getLogger().error(e)
            
        wait_on(
            lambda: summary_queue.full(),
            wait_message="Waiting on worker summaries {}",
            timeout=timeout,
            wait=True,
            timeout_callback=summary_timeout_callback)
        
        # Process summary information from worker processes
        logging.getLogger().debug("Processing summary information from worker processes")
        seen_summaries = set()
        seen_batches = set()
        
        def summary_fail_callback():
            missing_summaries = set(range(1, threads)) - seen_summaries
            raise Exception("Missing summaries from processes {}".format(
                ",".join(str(s) for s in missing)))
        
        for i in range(1, threads+1):
            batch = dequeue(
                summary_queue,
                fail_callback=summary_fail_callback)
            worker_index, worker_batches, process_stats, adapter_stats = batch
            if process_stats is None or adapter_stats is None:
                raise Exception("Worker process {} died unexpectedly".format(worker_index))
            else:
                logging.getLogger().debug("Processing summary for worker {}".format(worker_index))
            seen_summaries.add(worker_index)
            seen_batches |= worker_batches
            summary.add_process_stats(process_stats)
            summary.add_adapter_stats(adapter_stats)
        
        # Check if any batches were missed
        if num_batches > 0:
            missing_batches = set(range(1, num_batches+1)) - seen_batches
            if len(missing_batches) > 0:
                raise Exception("Workers did not process batches {}".format(
                    ",".join(str(b) for b in missing_batches)))
        
        if use_writer_process:
            # Wait for writer to complete
            wait_on_process(writer_process, timeout)
    
    try:
        rc = run_interruptible(_run, worker_processes)
    finally:
        # notify all threads that they should stop
        logging.getLogger().debug("Exiting all processes")
        def kill(process):
            if rc <= 1:
                wait_on_process(process, timeout, terminate=True)
            elif process.is_alive():
                process.terminate()
        for process in worker_processes:
            kill(process)
        if use_writer_process:
            kill(writer_process)
    
    report = summary.finish() if rc == 0 else None
    details = dict(mode='parallel', hreads=threads)
    return (rc, report, details)
