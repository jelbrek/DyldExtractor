import argparse
import io
import logging
import mmap
import multiprocessing
import pathlib
import signal
import sys

import progressbar

from DyldExtractor.converter import (
	linkedit_optimizer,
	macho_offset,
	objc_fixer,
	slide_info,
	stub_fixer
)
from DyldExtractor.dyld.dyld_context import DyldContext
from DyldExtractor.extraction_context import ExtractionContext
from DyldExtractor.macho.macho_context import MachOContext

# check dependencies
try:
	assert sys.version_info >= (3, 9, 5)
except AssertionError:
	print("Python 3.9.5 or greater is required", file=sys.stderr)
	exit(1)

try:
	progressbar.streams
except AttributeError:
	print("progressbar is installed but progressbar2 required.", file=sys.stderr)
	exit(1)


class _DyldExtractorArgs(argparse.Namespace):

	dyld_path: pathlib.Path
	output: pathlib.Path
	verbosity: int
	pass


def _createArgParser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Extract all images from a Dyld Shared Cache.")  # noqa
	parser.add_argument(
		"dyld_path",
		type=pathlib.Path,
		help="A path to the target DYLD cache."
	)
	parser.add_argument(
		"-o", "--output",
		type=pathlib.Path,
		help="Specify the output path for the extracted frameworks. By default it extracts to './binaries/'."  # noqa
	)
	parser.add_argument(
		"-v", "--verbosity",
		choices=[0, 1, 2, 3],
		default=1,
		type=int,
		help="Increase verbosity, Option 1 is the default. | 0 = None | 1 = Critical Error and Warnings | 2 = 1 + Info | 3 = 2 + debug |"  # noqa
	)

	return parser


class _DummyProgressBar():
	def update(*args, **kwargs):
		pass
	pass


def _workerInitializer():
	"""
	Ignore KeyboardInterrupt in workers so that the main process
	can receive it and stop everything.
	"""
	signal.signal(signal.SIGINT, signal.SIG_IGN)
	pass


def _extractImage(
	dyldPath: pathlib.Path,
	outputDir: pathlib.Path,
	imageIndex: int,
	imagePath: str,
	loggingLevel: int
) -> str:
	# change imagePath to a relative path
	if imagePath[0] == "/":
		imagePath = imagePath[1:]
		pass

	outputPath = outputDir / imagePath

	# setup logging
	logger = logging.getLogger(f"Worker: {outputPath}")

	loggingStream = io.StringIO()
	handler = logging.StreamHandler(loggingStream)
	formatter = logging.Formatter(
		fmt="{asctime}:{msecs:03.0f} [{levelname:^9}] {filename}:{lineno:d} : {message}",  # noqa
		datefmt="%H:%M:%S",
		style="{",
	)

	handler.setFormatter(formatter)
	logger.addHandler(handler)
	logger.setLevel(loggingLevel)

	# Process the image
	with open(dyldPath, "rb") as f:
		dyldFile = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
		dyldCtx = DyldContext(dyldFile)
		imageOffset = dyldCtx.convertAddr(dyldCtx.images[imageIndex].address)

		machoFile = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_COPY)
		machoCtx = MachOContext(machoFile, imageOffset)

		extractionCtx = ExtractionContext(
			dyldCtx,
			machoCtx,
			_DummyProgressBar(),
			logger
		)

		try:
			slide_info.processSlideInfo(extractionCtx)
			linkedit_optimizer.optimizeLinkedit(extractionCtx)
			stub_fixer.fixStubs(extractionCtx)
			objc_fixer.fixObjC(extractionCtx)
			macho_offset.optimizeOffsets(extractionCtx)

			# write the file
			outputPath.parent.mkdir(parents=True, exist_ok=True)
			with open(outputPath, "wb+") as outFile:
				newMachoCtx = extractionCtx.machoCtx

				# get the size of the new file
				linkeditSeg = newMachoCtx.segments[b"__LINKEDIT"].seg
				fileSize = linkeditSeg.fileoff + linkeditSeg.filesize

				newMachoCtx.file.seek(0)
				outFile.write(newMachoCtx.file.read(fileSize))
				pass
			pass
		except Exception as e:
			logger.exception(e)
			pass
		pass

	handler.close()
	loggingStream.flush()
	loggingOutput = loggingStream.getvalue()
	loggingStream.close()
	return loggingOutput


def _main() -> None:
	argParser = _createArgParser()
	args = argParser.parse_args(namespace=_DyldExtractorArgs())

	# Make the output dir
	if args.output is None:
		outputDir = pathlib.Path("binaries")
		pass
	else:
		outputDir = pathlib.Path(args.output)
		pass

	outputDir.mkdir(parents=True, exist_ok=True)

	if args.verbosity == 0:
		# Set the log level so high that it doesn't do anything
		loggingLevel = 100
	elif args.verbosity == 1:
		loggingLevel = logging.WARNING
	elif args.verbosity == 2:
		loggingLevel = logging.INFO
	elif args.verbosity == 3:
		loggingLevel = logging.DEBUG

	# create a list of image paths
	imagePaths: list[str] = []
	with open(args.dyld_path, "rb") as f:
		dyldFile = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
		dyldCtx = DyldContext(dyldFile)

		for image in dyldCtx.images:
			imagePath = dyldCtx.readString(image.pathFileOffset)[0:-1].decode("utf-8")
			imagePaths.append(imagePath)
			pass
		pass

	with multiprocessing.Pool(initializer=_workerInitializer) as pool:
		# Create a job for each image
		jobs: list[tuple[str, multiprocessing.pool.AsyncResult]] = []
		jobsComplete = 0
		for i, imagePath in enumerate(imagePaths):
			# The index should correspond with its index in the DSC
			extractionArgs = (args.dyld_path, outputDir, i, imagePath, loggingLevel)
			jobs.append((imagePath, pool.apply_async(_extractImage, extractionArgs)))
			pass

		# setup a progress bar
		progressBar = progressbar.ProgressBar(
			max_value=len(jobs),
			redirect_stdout=True
		)

		# Record potential logging output for each job
		jobOutputs: list[str] = []

		# wait for all jobs
		while len(jobs):
			for i in reversed(range(len(jobs))):
				imagePath, job = jobs[i]
				if job.ready():
					jobs.pop(i)

					imageName = imagePath.split("/")[-1]
					print(f"Processed: {imageName}")

					jobOutput = job.get()
					if jobOutput:
						summary = f"----- {imageName} -----\n{jobOutput}--------------------\n"
						jobOutputs.append(summary)
						print(summary)
						pass

					jobsComplete += 1
					progressBar.update(jobsComplete)
					pass
				pass
			pass

		# close the pool and cleanup
		pool.close()
		pool.join()
		progressBar.update(jobsComplete, force=True)

		# reprint any job output
		print("\n\n----- Summary -----")
		print("".join(jobOutputs))
		print("-------------------\n")
		pass
	pass


if __name__ == "__main__":
	_main()
