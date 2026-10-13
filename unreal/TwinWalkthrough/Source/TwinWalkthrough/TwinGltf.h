#pragma once

#include "CoreMinimal.h"
#include "ProceduralMeshComponent.h"

class IImageWrapperModule;
class FJsonObject;

/**
 * Minimal glTF 2.0 binary (GLB) reader for the twin's own Blender exports, used by the packaged viewer
 * (no editor, no importer plugins). Runs on a worker thread: it creates no UObjects.
 *
 * Supported: triangle primitives with POSITION / NORMAL / TEXCOORD_0 (float) and u8/u16/u32 indices,
 * node TRS or matrix hierarchies, metallic-roughness materials (base colour factor + texture, metallic,
 * roughness, emissive, alpha mode), PNG/JPEG images embedded in the binary chunk. Anything else fails
 * with a clear message instead of rendering wrongly.
 *
 * Coordinates: glTF is metres, right-handed, +Y up. Unreal is centimetres, left-handed, +Z up:
 *     p_ue = 100 * (x, z, y),  n_ue = (nx, nz, ny),  node transforms T_ue = C T_g C^-1
 * Swapping two axes mirrors the frame, which also turns glTF's counter-clockwise front faces into
 * Unreal's front-face winding, so index order is kept.
 */
namespace TwinGltf
{
	struct FMesh
	{
		TArray<FProcMeshSection> Sections;  // one per glTF primitive, in Unreal space (cm)
		TArray<int32> Materials;            // glTF material index per section (-1 = default)
		FBox LocalBox = FBox(ForceInit);
		int32 Triangles = 0;
	};

	struct FNode
	{
		FString Name;
		int32 Mesh = INDEX_NONE;
		FTransform Transform;               // world transform in Unreal space
		TSharedPtr<FJsonObject> Extras;
	};

	struct FImage
	{
		FString Name;
		int32 Width = 0;
		int32 Height = 0;
		TArray<TArray64<uint8>> Mips;       // BGRA8, mip 0 first
	};

	struct FMaterial
	{
		FString Name;
		FLinearColor BaseColor = FLinearColor::White;  // linear, alpha = opacity
		int32 BaseColorImage = INDEX_NONE;
		float Metallic = 1.f;
		float Roughness = 1.f;
		FLinearColor Emissive = FLinearColor::Black;
		bool bBlend = false;
		bool bDoubleSided = false;
	};

	struct FModel
	{
		TArray<FMesh> Meshes;
		TArray<FNode> Nodes;
		TArray<FImage> Images;
		TArray<FMaterial> Materials;
		int64 Triangles = 0;                // including instances
		FString Generator;
	};

	/** Parse a GLB. Returns false and sets OutError on any unsupported or malformed content. */
	bool Parse(const TArray<uint8>& Glb, IImageWrapperModule* ImageWrappers, FModel& Out, FString& OutError);

	/** glTF (metres, Y up) -> Unreal (cm, Z up) for a point. */
	inline FVector ToUnrealPoint(double X, double Y, double Z) { return FVector(X * 100.0, Z * 100.0, Y * 100.0); }
	inline FVector ToUnrealDir(double X, double Y, double Z) { return FVector(X, Z, Y); }
}
