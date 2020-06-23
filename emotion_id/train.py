from math import inf
import numpy as np
from pathlib import Path

import torch
from absl import flags, logging, app
from torch import save
import torch.nn.functional as F
from torch.nn.utils import clip_grad_norm_

import io
from PIL import Image
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    accuracy_score,
)
import seaborn as sns
import matplotlib.pyplot as plt

from emotion_id.model import (
    MLPEmotionIDModel,
    ConvEmotionIDModel,
    BaselineEmotionIDModel,
    LinearEmotionIDModel,
    RecurrentEmotionIDModel,
    WaveNetEmotionIDModel,
)
from emotion_id.dataset import (
    EmotionIDSingleFileStream,
    parse_emotion_dbl,
    get_emotion_to_id_mapping,
)
from dataloader.streaming import MultiStreamDataLoader, DblStream, DblSampler
from cpc.model import NoCPC

from util import (
    set_seeds,
    prepare_tb_logging,
    prepare_standard_logging,
    load_model,
    FixedRandomState,
    RAdam,
    FlatCA,
    resample_1d,
    device,
    setup_dry_run,
)

FLAGS = flags.FLAGS
flags.DEFINE_string("expdir", None, "directory to write all experiment data to")
flags.DEFINE_string("train_data", None, "path to train files")
flags.DEFINE_string("val_data", None, "path to validation files")
flags.DEFINE_string("test_data", None, "path to validation files")
flags.DEFINE_string("emotion_set_path", None, "path to smotion set")

flags.DEFINE_string("cpc_path", None, "path to cpc model to use")
flags.DEFINE_string("model_out", None, "path to where to save trained model")
flags.DEFINE_enum(
    "model",
    "mlp2",
    ["linear", "baseline", "mlp2", "mlp4", "conv", "rnn", "rnn_bi", "wavenet", "wavenet_unmasked"],
    "The model type",
)

flags.DEFINE_integer("window_size", 2048, "num frames to push into model at once")
flags.DEFINE_integer("batch_size", None, "batch size, num parallel streams to train on at once")
flags.DEFINE_integer("steps", None, "number of train steps before breaking")
flags.DEFINE_integer("hidden_size", 1024, "hidden size for models")
flags.DEFINE_float("dropout_prob", 0.0, "dropout probability")
flags.DEFINE_float("lr", 4e-4, "learning rate")
flags.DEFINE_float("clip_thresh", 1.0, "value to clip gradients to")

flags.DEFINE_integer("valid_steps", None, "number of steps to take in validation")
flags.DEFINE_integer("val_every", None, "how often to perform validation")
flags.DEFINE_integer("save_every", None, "save every n steps")

flags.DEFINE_boolean(
    "lr_schedule", False, "state if an learning rate scheduler is required during training",
)
flags.DEFINE_boolean("dry_run", False, "dry run")
flags.DEFINE_boolean("batch_norm", False, "batch_norm")


flags.mark_flag_as_required("emotion_set_path")
flags.mark_flag_as_required("batch_size")
flags.mark_flag_as_required("steps")
flags.mark_flag_as_required("train_data")
flags.mark_flag_as_required("val_data")
flags.mark_flag_as_required("expdir")


def fig2tensor(fig):
    """Convert a Matplotlib figure to a PIL Image and return it"""
    buf = io.BytesIO()
    fig.savefig(buf)
    buf.seek(0)
    img = Image.open(buf)
    x = np.array(img)
    x = torch.Tensor(x).permute(2, 0, 1) / 255.0
    return x


def validate(datastream, cpc, model, num_emotions):
    losses = []
    model.eval()

    # Stash and later restore states for non-leaky validation
    cpc.stash_state()
    model.stash_state()

    # reset to a fixed random seed for determisitic and comparable validation
    with FixedRandomState(42):
        for step, batch in enumerate(datastream):
            data, labels = batch["data"].to(device), batch["labels"]
            with torch.no_grad():
                features = cpc(data)
                pred = model(features)
                labels = resample_1d(labels, pred.shape[1])
                pred = pred.reshape(-1, num_emotions)
                labels = labels.reshape(-1)
                losses.append(F.cross_entropy(pred, labels.to(device)).item())
            if step >= FLAGS.valid_steps:
                break
    cpc.pop_state()
    model.pop_state()

    model.train()
    return np.array(losses).mean()


def validate_filewise(dbl, cpc, model, num_emotions):
    logging.info("Starting filewise validation")
    losses = []
    preds = []
    refs = []
    model.eval()

    # Stash and later restore states for non-leaky validation
    cpc.stash_state()
    model.stash_state()

    # loop over each dbl
    for i, dbl_entry in enumerate(dbl):
        # file specific stream to iterate over
        stream = EmotionIDSingleFileStream(
            dbl_entry, FLAGS.window_size, FLAGS.emotion_set_path, audiostream_class=cpc.data_class
        )
        for j, batch in enumerate(stream):
            with torch.no_grad():
                data = torch.tensor(batch["data"]).unsqueeze(0).to(device)
                labels = torch.tensor(batch["labels"]).unsqueeze(0)
                # get predictions
                features = cpc(data)
                logits = model(features)
                # get pred
                pred = logits.argmax(dim=2).squeeze(dim=0)
                preds.append(pred)
                if logits.shape[1] > 1:
                    labels = resample_1d(labels, logits.shape[1])
                labels = labels.reshape(-1)
                refs.append(labels)
                # get loss
                logits = logits.reshape(-1, num_emotions)
                losses.append(F.cross_entropy(logits, labels.to(device)).item())

    preds = torch.cat(preds, dim=0).cpu().numpy()
    refs = torch.cat(refs, dim=0).cpu().numpy()

    av_loss = np.array(losses).mean()
    acc = accuracy_score(refs, preds)
    f1 = f1_score(refs, preds, average="macro")

    cm = confusion_matrix(refs, preds)
    fig, ax = plt.subplots(figsize=(20, 16), dpi=150)
    sns.heatmap(cm, annot=True, ax=ax)
    ax.set_xlabel("Predicted labels")
    ax.set_ylabel("True labels")

    cpc.pop_state()
    model.pop_state()
    model.train()

    return av_loss, acc, f1, fig


def train(unused_argv):
    set_seeds(FLAGS.seed)
    # setup logging
    tb_logger = prepare_tb_logging()
    prepare_standard_logging("training")
    loss_dir = Path(f"{FLAGS.expdir}/losses")
    loss_dir.mkdir(exist_ok=True)
    train_losses_fh = open(loss_dir / "train.txt", "a")
    valid_losses_fh = open(loss_dir / "valid.txt", "a")

    if FLAGS.dry_run is True:
        setup_dry_run(FLAGS)
    if not FLAGS.model_out:
        FLAGS.model_out = FLAGS.expdir + "/model.pt"

    if FLAGS.cpc_path is not None:
        cpc = load_model(FLAGS.cpc_path).to(device)
        cpc.reset_state()
    else:
        cpc = NoCPC()
    cpc.eval()

    # write information about cpc into metadata
    with open(f"{FLAGS.expdir}/metadata.txt", "a") as fh:
        fh.write(f"sampling_rate_hz {cpc.data_class.SAMPLING_RATE_HZ}\n")
        fh.write(f"feat_dim {cpc.feat_dim}\n")

    # define training data
    parsed_train_dbl = parse_emotion_dbl(FLAGS.train_data)
    train_streams = [
        DblStream(
            DblSampler(parsed_train_dbl),
            EmotionIDSingleFileStream,
            FLAGS.window_size,
            emotion_set_path=FLAGS.emotion_set_path,
            audiostream_class=cpc.data_class,
        )
        for _ in range(FLAGS.batch_size)
    ]
    train_datastream = MultiStreamDataLoader(train_streams, device=device)
    # define validation data
    parsed_valid_dbl = parse_emotion_dbl(FLAGS.val_data)
    parsed_test_dbl = parse_emotion_dbl(FLAGS.test_data)
    val_streams = [
        DblStream(
            DblSampler(parsed_valid_dbl),
            EmotionIDSingleFileStream,
            FLAGS.window_size,
            emotion_set_path=FLAGS.emotion_set_path,
            audiostream_class=cpc.data_class,  # TODO ensure un-augmented stream
        )
        for _ in range(FLAGS.batch_size)
    ]
    valid_datastream = MultiStreamDataLoader(val_streams, device=device)
    if not FLAGS.val_every:
        FLAGS.val_every = max(100, FLAGS.steps // 50)
    if not FLAGS.save_every:
        FLAGS.save_every = FLAGS.val_every
    if not FLAGS.valid_steps:
        FLAGS.valid_steps = max(20, FLAGS.val_every // 100)
    valid_frames = FLAGS.batch_size * FLAGS.window_size * FLAGS.valid_steps

    feat_dim = cpc.feat_dim
    num_emotions = len(get_emotion_to_id_mapping(FLAGS.emotion_set_path))

    if FLAGS.model == "linear":
        model = LinearEmotionIDModel(feat_dim, num_emotions).to(device)
    if FLAGS.model == "baseline":
        model = BaselineEmotionIDModel(feat_dim, num_emotions).to(device)
    elif FLAGS.model == "mlp2":
        model = MLPEmotionIDModel(
            feat_dim,
            num_emotions,
            no_layers=2,
            hidden_size=FLAGS.hidden_size,
            dropout_prob=FLAGS.dropout_prob,
            batch_norm_on=FLAGS.batch_norm,
        ).to(device)
    elif FLAGS.model == "mlp4":
        model = MLPEmotionIDModel(
            feat_dim,
            num_emotions,
            no_layers=4,
            hidden_size=FLAGS.hidden_size,
            dropout_prob=FLAGS.dropout_prob,
            batch_norm_on=FLAGS.batch_norm,
        ).to(device)
    elif FLAGS.model == "conv":
        model = ConvEmotionIDModel(
            feat_dim,
            num_emotions,
            no_layers=4,
            hidden_size=FLAGS.hidden_size,
            dropout_prob=FLAGS.dropout_prob,
        ).to(device)
    elif FLAGS.model == "rnn":
        model = RecurrentEmotionIDModel(
            feat_dim=feat_dim,
            num_emotions=num_emotions,
            bidirectional=False,
            hidden_size=FLAGS.hidden_size,
            dropout_prob=FLAGS.dropout_prob,
        ).to(device)
    elif FLAGS.model == "rnn_bi":
        model = RecurrentEmotionIDModel(
            feat_dim=feat_dim,
            num_emotions=num_emotions,
            bidirectional=True,
            hidden_size=FLAGS.hidden_size,
            dropout_prob=FLAGS.dropout_prob,
        ).to(device)
    elif FLAGS.model == "wavenet":
        model = WaveNetEmotionIDModel(feat_dim, num_emotions).to(device)
        padding_percentage = 100 * model.max_padding / FLAGS.window_size
        logging.info(f"max padding {model.max_padding}, percentage {padding_percentage}%")
        logging.info(f"receptve field {model.receptive_field}")
    elif FLAGS.model == "wavenet_unmasked":
        model = WaveNetEmotionIDModel(feat_dim, num_emotions, masked=False).to(device)
        padding_percentage = 100 * model.max_padding / FLAGS.window_size
        logging.info(f"max padding {model.max_padding}, percentage {padding_percentage}%")
        logging.info(f"receptve field {model.receptive_field}")
    else:
        raise NameError("Model name not found")

    logging.info(f"number of classes {num_emotions}")
    logging.info(f"model param count {sum(x.numel() for x in model.parameters()):,}")

    optimizer = RAdam(model.parameters(), eps=1e-05, lr=FLAGS.lr)
    if FLAGS.lr_schedule:
        scheduler = FlatCA(optimizer, steps=FLAGS.steps, eta_min=0)

    best_val_loss = inf
    for step, batch in enumerate(train_datastream):
        data, labels = batch["data"].to(device), batch["labels"]
        features = cpc(data)
        pred = model(features)
        labels = resample_1d(labels, pred.shape[1]).reshape(-1).to(device)

        # get cross entropy loss against emotion labels and take step
        optimizer.zero_grad()
        output = model(features).reshape(-1, num_emotions)
        loss = F.cross_entropy(output, labels)
        loss.backward()
        clip_grad_norm_(model.parameters(), FLAGS.clip_thresh)

        optimizer.step()
        if FLAGS.lr_schedule:
            scheduler.step()
        # log training losses
        logging.info(f"{step} train steps, loss={loss.item():.5}")
        tb_logger.add_scalar("train/loss", loss, step)
        train_losses_fh.write(f"{step}, {loss.item()}\n")

        if FLAGS.lr_schedule:
            tb_logger.add_scalar("train/lr", scheduler.get_lr()[0], step)

        # validate periodically
        if step % FLAGS.val_every == 0 and step != 0:

            valid_loss = validate(valid_datastream, cpc, model, num_emotions)
            # log validation losses
            logging.info(
                f"{step} validation, loss={valid_loss.item():.5}, "
                f"{valid_frames:,} items validated"
            )
            tb_logger.add_scalar("valid/loss", valid_loss, step)
            valid_losses_fh.write(f"{step}, {valid_loss}\n")

            av_loss, acc, f1_score, fig = validate_filewise(
                parsed_valid_dbl, cpc, model, num_emotions
            )
            tb_logger.add_scalar("valid/full_loss", av_loss, step)
            tb_logger.add_scalar("valid/accuracy", acc, step)
            tb_logger.add_scalar("valid/f1_score", f1_score, step)
            tb_logger.add_image("valid/confusion_matrix", fig2tensor(fig), step)

            av_loss, acc, f1_score, fig = validate_filewise(
                parsed_test_dbl, cpc, model, num_emotions
            )
            tb_logger.add_scalar("test/full_loss", av_loss, step)
            tb_logger.add_scalar("test/accuracy", acc, step)
            tb_logger.add_scalar("test/f1_score", f1_score, step)
            tb_logger.add_image("test/confusion_matrix", fig2tensor(fig), step)

            if valid_loss.item() < best_val_loss:
                logging.info("Saving new best validation")
                save(model, FLAGS.model_out + ".bestval")
                best_val_loss = valid_loss.item()

        # save out model periodically
        if step % FLAGS.save_every == 0 and step != 0:
            save(model, FLAGS.model_out + ".step" + str(step))

        if step >= FLAGS.steps:
            break

    save(model, FLAGS.model_out)

    # close loss logging file handles
    train_losses_fh.close()
    valid_losses_fh.close()


if __name__ == "__main__":
    app.run(train)
